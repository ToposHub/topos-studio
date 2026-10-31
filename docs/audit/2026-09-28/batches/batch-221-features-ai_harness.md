# 批次 batch-221 审查报告：src/features/ai_harness（MCP 传输 / 插件发现·隔离·持久化）

审查区间：AUD-22100–AUD-22199。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批次为 AI 插件宿主基础设施（subprocess、multiprocessing、文件/网络代理、项目持久化），不含 OpenGL/Qt 代码，GPU/GLSL/信号防递归维度记为不适用。

---

## src/features/ai_harness/mcp_protocol.py (行数: 291)

### 发现

- [AUD-22101] [Major] 不可信 JSON Schema 可导致校验线程资源耗尽（组合爆炸 + 正则回溯）
  - 位置: src/features/ai_harness/mcp_protocol.py:159-174
  - 证据:
    ```python
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        variants = schema[keyword]
        if not isinstance(variants, list) or not variants:
            return f"{path}: invalid {keyword} schema"
        errors = [validate_json_schema(
            value, item, path=path, max_depth=max_depth - 1,
            _root_schema=root_schema,
        ) for item in variants]
    ```
    以及 229-235 行：
    ```python
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, value) is None:
                return f"{path}: string does not match pattern"
    ```
  - 影响: schema 来自外部 MCP server 的 `tools/list`（不可信数据，模块自述用于 "MCP tool boundaries"）。`max_depth=32` 只限制嵌套深度，不限制 variants 数量；`anyOf`/`oneOf` 嵌套时对同一 value 全量求值（allOf 也总是算完所有分支），复杂度为分支数^深度级别；`re.search` 使用 server 提供的 pattern，Python `re` 无超时，可被回溯型 pattern 挂起。调用方 `McpOutboundClient.call_tool`（mcp_outbound.py:362）在调用线程上执行校验，恶意/损坏的 server 可使该线程长时间无响应。
  - 建议: 为 schema 增加尺寸/复杂度上限（节点数、variants 数量、总深度乘积预算），在校验入口计数并提前拒绝；`pattern` 校验前限制 pattern 长度并考虑改用 `re.fullmatch` 前的静态风险检查或子进程/超时隔离。
  - 置信度: 中（代码路径确定；实际触发需 server 提供恶意 schema，而 server 需 host allowlist）

- [AUD-22106] [Suggestion] SlidingWindowRateLimiter.allow 为非原子的 check-then-append，并发下限流可被轻微突破
  - 位置: src/features/ai_harness/mcp_protocol.py:269-274
  - 证据:
    ```python
    while self._calls and self._calls[0] <= cutoff:
        self._calls.popleft()
    if len(self._calls) >= self.max_calls:
        return False
    self._calls.append(current)
    ```
  - 影响: `McpOutboundClient.call_tool`（mcp_outbound.py:406-409）无锁调用 `limiter.allow()`；多线程并发调用时 check 与 append 之间可交错，超出 `max_calls` 若干次。限流是滥用防线，超限幅度小，不破坏数据完整性。
  - 建议: 在 `allow` 内部加 `threading.Lock`（或由调用方在锁内使用），使判断+记录原子化。
  - 置信度: 高

- [AUD-22107] [Suggestion] enum 校验中 bool 与 int 相等（True == 1）偏离 JSON Schema 语义
  - 位置: src/features/ai_harness/mcp_protocol.py:146-149
  - 证据:
    ```python
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(value == candidate for candidate in enum):
            return f"{path}: value is not one of the allowed values"
    ```
  - 影响: `value=True` 会匹配 `enum: [1]`（`_json_type_matches` 已专门排除 bool，说明意图是区分二者），enum 分支未做同样的 bool/int 区分，校验语义不一致。
  - 建议: 比较时加入类型一致性检查，如 `any(value == c and isinstance(value, bool) == isinstance(c, bool) for c in enum)` 或先按类型分组。
  - 置信度: 高

### 通过项
- 线程安全：模块级纯函数无可变共享状态（限流器问题单列）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（无 OpenGL/Qt）
- 输入验证：`validate_tool_definition`/`validate_tool_result` 对不可信结构逐字段验证，`$ref` 仅允许本地指针且解析失败被拒绝，`~0`/`~1` 转义正确
- 异常路径：`re.error` 被捕获转为结构化错误而非崩溃
- 类型注解完整

---

## src/features/ai_harness/mcp_stdio_transport.py (行数: 342)

### 发现

- [AUD-22102] [Major] close() 后立即 start() 时，旧 stdout 泄放线程可能向新队列投毒 None 哨兵
  - 位置: src/features/ai_harness/mcp_stdio_transport.py:183-196, 126
  - 证据:
    ```python
    def _drain_stdout(self) -> None:
        proc = self._proc
        ...
        try:
            for raw in iter(proc.stdout.readline, b""):
                message = self._parse(raw)
                if message is not None:
                    self._stdout_queue.put(message)
        except Exception:  # noqa: BLE001 — child shutdown
            pass
        finally:
            self._stdout_queue.put(None)
    ```
    而 `_spawn`（126 行）`self._stdout_queue = queue.Queue()` 会替换为新队列。
  - 影响: `_drain_stdout` 的 `finally` 读取的是**当前属性** `self._stdout_queue` 而非线程启动时捕获的引用。close() 终止旧进程后旧线程从 readline 惊醒并 `put(None)`；若此刻已重新 start()（close() 显式支持重启：`_started=False`），None 落入**新**队列，`_roundtrip` 会误判 "MCP 子进程已退出 / 无响应"，导致重启失败并杀掉新进程。
  - 建议: 在 `_spawn` 时把队列捕获为局部变量传入线程（`args=(q,)`），或在消息中携带代际计数/generation id，`_roundtrip` 只接受当前代的哨兵。
  - 置信度: 中（代码路径确定，触发依赖 close→start 的时间窗口）

- [AUD-22103] [Major] send_request 全程持有 RLock，close() 最长阻塞 request_timeout（默认 180s）
  - 位置: src/features/ai_harness/mcp_stdio_transport.py:202-220, 255-276
  - 证据:
    ```python
    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._started:
            self.start()
        with self._lock:
            ...
            response = self._roundtrip(prepared, timeout=self._request_timeout)
    ```
    ```python
    def close(self) -> None:
        with self._lock:
            self._started = False
            ...
    ```
  - 影响: `_roundtrip` 在 server 无响应时会等满超时（默认 180s）才抛错。期间任何线程调用 `close()`（mcp_vision_gateway.shutdown → McpOutboundClient.close 遍历 transports）都会在锁上阻塞最长 180s；若 shutdown 发生在应用退出路径，表现为退出卡死。
  - 建议: `close()` 不走同一把锁：置位关闭标志 + 直接 `_terminate_current_process()`（终止子进程会使阻塞中的 `_roundtrip` 立即收到 None/EOF 而失败返回）；或为 `_roundtrip` 提供可被关闭事件中断的等待。
  - 置信度: 中

- [AUD-22104] [Major] `_stdout_queue` 无界，空闲期间 server 通知可无限累积
  - 位置: src/features/ai_harness/mcp_stdio_transport.py:189-192, 270
  - 证据:
    ```python
    for raw in iter(proc.stdout.readline, b""):
        message = self._parse(raw)
        if message is not None:
            self._stdout_queue.put(message)
    ```
  - 影响: 消费只发生在 `_roundtrip` 等待期间（非匹配消息被丢弃）；两次请求之间无人消费。行为异常的 server 持续向 stdout 输出 JSON 通知（如 `notifications/message` 日志流）时，队列内存无上界增长（进程内 DoS）。
  - 建议: 使用 `queue.Queue(maxsize=...)` + 丢弃策略（丢弃最旧并计数），或在没有在途请求时由泄放线程直接丢弃通知类消息。
  - 置信度: 中

- [AUD-22108] [Suggestion] server 发起的 JSON-RPC 请求被静默丢弃，未回 MethodNotFound
  - 位置: src/features/ai_harness/mcp_stdio_transport.py:281-283
  - 证据:
    ```python
    if message.get("id") == req_id:
        return message
    _LOG.debug("mcp 忽略非匹配消息: %s", message.get("method") or message)
    ```
  - 影响: 带自有 id 的 server→client 请求（如 `sampling/createMessage`、`roots/list`）只被 debug 日志丢弃；遵守规范的 server 可能阻塞等待答复，间接拖垮后续交互（与超时叠加）。
  - 建议: 对带 `id` 且含 `method` 的消息回送 JSON-RPC error(-32601) 通知，或至少在文档中明确不支持 server 发起请求。
  - 置信度: 中

- [AUD-22109] [Suggestion] 重启后 `_stderr_lines` 不清空，诊断信息混入上一进程内容
  - 位置: src/features/ai_harness/mcp_stdio_transport.py:73, 126-134
  - 证据:
    ```python
    self._stderr_lines: list[str] = []
    ...
    def _spawn(self) -> None:
        ...
        self._stdout_queue = queue.Queue()
        self._stderr_thread = threading.Thread(...)
    ```
  - 影响: `_spawn` 重置了队列但未重置 `_stderr_lines`；close→start 后 `_stderr_tail()` 把旧进程 stderr 拼进新进程错误消息，误导排障。
  - 建议: 在 `_spawn` 中同步 `self._stderr_lines = []`。
  - 置信度: 高

### 通过项
- 启动竞态：`start()` 持锁二次检查 `_started`，重复启动被挡住；启动失败路径统一 `_terminate_current_process()` 后重抛
- 子进程生命周期：terminate→wait(3)→kill→wait(3) 兜底，三个流在 finally 关闭；泄放线程为 daemon 且 EOF 后退出
- 异常与超时路径：`_roundtrip` 超时先 best-effort `notifications/cancelled` 再抛错，不掩盖原始错误；`_write` 捕获 BrokenPipe/OSError 转为传输错误；`_parse` 拒绝非 JSON/非对象行
- 输入验证：空 command 拒绝；modern/legacy 探测失败自动回退 legacy 握手，握手无 result 时报错含 stderr 上下文
- `_build_env` 仅在绝对路径命令时 prepend PATH，不意外继承额外注入
- 类型注解完整

---

## src/features/ai_harness/plugin_broker.py (行数: 497)

### 发现

- [AUD-22110] [Suggestion] `PathGrant.resolved_root` 每次检查都重新 expanduser+resolve（每次 I/O 多次系统调用）
  - 位置: src/features/ai_harness/plugin_broker.py:387-389, 447-448
  - 证据:
    ```python
    @property
    def resolved_root(self) -> str:
        return str(Path(self.root).expanduser().resolve(strict=False))
    ```
    ```python
    for grant in self._path_grants.get(plugin_id, ()):
        root = grant.resolved_root
    ```
  - 影响: `check_path` 是每次插件 read/write/network 前的必经路径，grant 数量多时每个 grant 每次都做路径解析（逐级 lstat），高频小读写场景产生可观测开销；且语义上 grant 根在构造后不应漂移。
  - 建议: 在 `__post_init__` 中用 `object.__setattr__` 缓存解析结果（frozen dataclass 惯用法）。
  - 置信度: 高（性能项，不影响正确性）

- [AUD-22111] [Suggestion] HTTPError 响应体未关闭，连接/FD 滞留至 GC
  - 位置: src/features/ai_harness/plugin_broker.py:295-296
  - 证据:
    ```python
    except urllib.error.HTTPError as exc:
        return HostIOResult(False, "PLUGIN_HTTP_ERROR", str(exc)[:500], status_code=int(exc.code or 0))
    ```
  - 影响: `HTTPError` 本身持有响应 fp；不 `close()` 也不读尽 body 时底层 socket 要等垃圾回收才释放。插件循环触发 4xx/5xx（如重试循环）会短时间累积连接。
  - 建议: 在返回前 `exc.close()`（finally 保护），或读取后关闭。
  - 置信度: 中

### 通过项
- 默认拒绝语义：`DefaultDenyResourceBroker` 全拒；`AllowlistResourceBroker` 未命中 grant/策略不允许时拒绝，方向为 fail-closed
- 路径安全：`expanduser().resolve()` 消解符号链接后按 `os.path.commonpath` 做根包含判断（组件级，无前缀绕过）；`O_NOFOLLOW` + `S_ISREG` + `O_EXCL/O_TRUNC` 组合正确；读路径 fstat 预检 + 逐块读取双重限制大小（TOCTOU 变大文件也被截断拒绝）
- 网络安全：拒绝重定向（`_NoRedirectHandler`）、禁用代理、URL 含凭词/userinfo 拒绝、scheme+域名+子域精确匹配、方法白名单 GET/POST、header 白名单并规范化大小写、Content-Length 预检 + `read(limit+1)` 双重限制
- `_bounded_limit` 显式排除 bool 且钳制到 `max_bytes`，调用方可传更小值不可放大
- 异常路径：文件/网络失败统一转为结构化 HostIOResult，不泄漏原始异常对象
- 线程安全：PluginHostIO 配置不可变、无共享可变状态
- 类型注解完整

---

## src/features/ai_harness/plugin_discovery.py (行数: 314)

### 发现

- [AUD-22112] [Suggestion] VERIFIED_THIRD_PARTY 分支中的二次哈希校验为不可达死代码
  - 位置: src/features/ai_harness/plugin_discovery.py:133-145, 174-186
  - 证据:
    ```python
    if manifest.declared_hash and not manifest.verify_declared_hash():
        return PluginDiscoveryCandidate(
            manifest=manifest,
            state=PluginDiscoveryState.REJECTED,
    ```
    （133 行已对所有来源做哈希不匹配拒绝）随后 174 行：
    ```python
    if not manifest.verify_declared_hash():
        return PluginDiscoveryCandidate(
            manifest=manifest,
            state=PluginDiscoveryState.REJECTED,
    ```
  - 影响: `PluginManifest` 为 frozen dataclass，`content_hash` 基于 sort_keys JSON 确定性计算；到达 174 行时 `declared_hash` 非空且 133 行校验已通过，`verify_declared_hash()` 必为 True，该分支永不可达，徒增阅读成本并暗示两处策略可能漂移。
  - 建议: 删除 174-186 行的重复检查（保留 133 行的统一检查与 161 行的缺哈希拒绝）。
  - 置信度: 高

### 通过项
- 无副作用设计：只接受已验证的 `PluginManifest`，不 import/执行插件代码；install 一律 `enabled=False`，不自动启用
- 信任链：已核实 `PluginSource` 仅 BUILTIN/LOCAL_DEV/VERIFIED_THIRD_PARTY（plugin_runtime.py:165-168）且 `PluginManifest.__post_init__` 对非法 source 抛错，末尾 READY 兜底分支只覆盖 BUILTIN，不存在"未知来源落入 READY 且免显式启用"的绕过
- 重复 ID：`discover` 对后续重复 ID 追加 PLUGIN_DUPLICATE_ID 并拒绝
- 输入验证：非 PluginManifest 入参直接 REJECTED
- 数据结构均为 frozen dataclass，无线程安全问题
- 类型注解完整

---

## src/features/ai_harness/plugin_isolation.py (行数: 761)

### 发现

- [AUD-22105] [Major] context.deadline 为 None（默认策略）且无取消源时，invoke 等待循环无任何看门狗，可永久占锁
  - 位置: src/features/ai_harness/plugin_isolation.py:515-549
  - 证据:
    ```python
    while True:
        if context is not None:
            if context.cancelled:
                self._terminate_locked()
                raise PluginInvocationCancelled("plugin invocation cancelled")
            if context.timed_out:
                self._terminate_locked()
                raise PluginInvocationTimedOut("plugin invocation deadline exceeded")
        message = self._poll_message_locked(0.02)
        if message is None:
            if not self._is_alive_locked():
                self._terminate_locked()
                raise PluginInvocationProcessCrashed(...)
            continue
    ```
  - 影响: 退出条件只有：收到消息 / 进程死亡 / 取消 / 超时。`PluginExecutionPolicy.timeout_seconds` 默认为 None（plugin_runtime.py:58），registry 侧 deadline 也随之为 None；`cancel_event` 可为 None。子进程活着但不回包（插件卡在阻塞调用）时，调用线程永久自旋在 20ms 轮询上，且全程持有 `self._lock`——同插件的 `health()`/`deactivate()`/`activate()` 一并永久阻塞。
  - 建议: 在 `invoke` 增加本地看门狗（如 policy.timeout_seconds 或进程级默认上限到期即 `_terminate_locked()` + 抛 `PluginInvocationTimedOut`），或至少在文档/构造参数中强制要求 deadline。
  - 置信度: 高（代码路径确定；实际影响取决于调用方是否总是传入 timeout/cancel 源）

- [AUD-22113] [Suggestion] 子进程 `_ProcessRPC.request` 不使用 remaining_seconds 本地超时，host 停滞时子进程无限等待
  - 位置: src/features/ai_harness/plugin_isolation.py:77-99
  - 证据:
    ```python
    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        _send(self._connection, {
            ...
            "remaining_seconds": self._remaining_seconds,
            **payload,
        })
        while True:
            if not self._connection.poll(0.02):
                continue
            message = self._connection.recv()
    ```
  - 影响: `remaining_seconds` 只发给 host 用于裁剪网络超时，子进程自身等待无界；host 线程停滞（非死亡）时插件在子进程中永久阻塞。host 死亡时依赖 daemon 进程连带退出，属可接受兜底。
  - 建议: 在子进程轮询循环中检查本地 deadline（`set_remaining_seconds` 已有值），到期抛 `PluginInvocationTimedOut` 让 invoke 返回结构化超时。
  - 置信度: 中

- [AUD-22114] [Suggestion] 普通插件异常被统一映射为 PluginInvocationProcessCrashed，误导诊断分类
  - 位置: src/features/ai_harness/plugin_isolation.py:569-575, 323-331
  - 证据:
    ```python
    code = str(message.get("code", "PLUGIN_PROCESS_HANDLER_FAILED"))
    error = str(message.get("error", "isolated plugin call failed"))[:500]
    if code == "PLUGIN_CANCELLED":
        raise PluginInvocationCancelled(error)
    if code == "PLUGIN_TIMEOUT":
        raise PluginInvocationTimedOut(error)
    raise PluginInvocationProcessCrashed(error)
    ```
    子进程侧普通异常 code 为 `PLUGIN_PROCESS_HANDLER_FAILED`（323-331 行）。
  - 影响: 插件 handler 抛出的普通 ValueError/KeyError 等在 host 侧统一变成 `PluginInvocationProcessCrashed`，registry 随即归档为 `PLUGIN_PROCESS_CRASHED` 诊断——但进程其实健在且可继续服务，错误分类与事实不符，排障时误导。
  - 建议: 为 `PLUGIN_PROCESS_HANDLER_FAILED` 单独引入 `PluginInvocationFailed` 类（或在异常消息中保留 code），仅对 IPC/协议层错误使用 ProcessCrashed。
  - 置信度: 高

### 通过项
- 进程生命周期：spawn 前 pickle 预检工厂；启动失败关闭管道并清理；`_terminate_locked` terminate→join(grace)→kill→join；`child.close()` 在 finally 中防 fd 泄漏；boot_error/ready 超时路径均收敛到 terminate
- sandbox：`required` 模式下 launcher 未证明 enforced 即拒绝激活，事件 sink 报告 degraded，不谎报隔离
- 子→宿能力：io_request 由 host 侧 `_handle_io_request_locked` 处理，使用 host 绑定的 plugin_id/policy，子进程传入的 id 不被信任；网络超时被 remaining 裁剪、remaining<=0 直接结构化超时
- 异常包含：worker 外层 `except BaseException` 上报 boot_error；取消/超时有专用异常类型；事件发送失败静默（best-effort 语义，注释说明）
- 取消路径：host 每 20ms 检查 cancelled/timed_out，命中即 terminate + 抛对应异常（不谎称已安全取消）
- 输入验证：`_poll_message_locked` 校验 version；worker 校验消息类型/操作名；返回值必须为 Mapping
- 线程安全：所有状态变更持 RLock；单飞行设计与 registry 信号量分工有注释说明
- 类型注解完整

---

## src/features/ai_harness/plugin_persistence.py (行数: 656)

### 发现

- [AUD-22115] [Suggestion] `restore` 仅精确匹配 "disabled" 才禁用，与 `restore_project_state` 的规范化处理不一致
  - 位置: src/features/ai_harness/plugin_persistence.py:191-197
  - 证据:
    ```python
    previous_state = str(raw.get("state", "enabled"))
    accepted = registry.register(
        manifest,
        plugin,
        enabled=previous_state != "disabled",
        policy=policy,
    )
    ```
    对比 `PluginRegistry.restore_project_state`（plugin_runtime.py:592）：
    ```python
    requested = str(persisted_state or "").strip().lower()
    ```
  - 影响: 持久化 state 为 `"Disabled"`、`"disabled "` 或任何未知串（含损坏值）时均按 enabled=True 注册；而 restore_project_state 会 strip+lower 并拒绝未知状态。两条恢复路径对同一持久化事实的判定不一致，`restore` 一侧偏 fail-open（虽仍需显式激活，风险有限）。
  - 建议: `restore` 同样 `strip().lower()`，并对未知状态记 skipped/conflict 而非默认启用。
  - 置信度: 高

- [AUD-22116] [Suggestion] journal 快照的 to_dict/rollback 为浅拷贝，调用方改动嵌套结构会污染检查点
  - 位置: src/features/ai_harness/plugin_persistence.py:409-415, 472-477
  - 证据:
    ```python
    def to_dict(self) -> dict[str, Any]:
        return {
            ...
            "snapshot": dict(self.snapshot),
        }
    ```
    ```python
    def rollback(self, checkpoint_id: str) -> Mapping[str, Any] | None:
        for checkpoint in self._checkpoints:
            if checkpoint.checkpoint_id == str(checkpoint_id):
                self._current_id = checkpoint.checkpoint_id
                return dict(checkpoint.snapshot)
    ```
  - 影响: `dict(...)` 一层浅拷贝，`plugins` 列表及其中 manifest dict 仍与检查点共享引用；调用方（或其下游 registry 操作）原地修改嵌套 dict 会篡改历史检查点，破坏回滚语义与哈希对应的快照内容。
  - 建议: 返回深拷贝（`copy.deepcopy` 或经 `sanitize_for_storage` 重建新容器，后者本就产生新 dict/list/tuple）。
  - 置信度: 高

### 通过项
- 授权不可持久化：export/migrate 强制 `approval_grants=[]`、`authorization_reset=True`，重启后需重新审批（模块红线达成）
- 完整性：`_manifest_from_dict` 重建后校验 content_hash，不匹配即拒；journal 每个检查点按 canonical JSON 重算 sha256，篡改即 skipped；migration 每步后重算 content_hash，不带陈旧哈希跨边界
- 脱敏一致性：export 对 sanitized manifest 重算哈希（与 `_stored_manifest_hash` 的 live 侧重算路径一致，已核对 `PluginRecord.to_dict` → `PluginManifest.to_dict` 输入同构）；`sanitize_for_storage` 返回全新容器，export 中的就地修改不污染 registry
- 迁移安全：`PluginMigrationRegistry.migrate` 有 seen_versions 环检测、单步异常隔离为 blocked 条目；目标版本缺失/迁移失败 fail-closed
- 状态恢复语义：restore 将 active 降级为需显式 reactivation；实现不可用时 degrade 而非假装健康；`PluginExecutionPolicy.__post_init__` 已验证正数/枚举，损坏 policy 值在 restore 中转为 skipped
- journal 有界：`[-max_checkpoints:]` 截断；from_dict 对 max_checkpoints 类型/取值全面防御
- 异常路径：migrate/restore 全部结构化报告，不抛出到项目保存链路（调用方 ai_edit_controller 另有兜底）
- 类型注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 5 | AUD-22101, AUD-22102, AUD-22103, AUD-22104, AUD-22105 |
| Suggestion | 11 | AUD-22106–AUD-22116 |

无 Critical 问题。Major 集中在两处：外部 MCP server（不可信进程）对宿主的资源耗尽/卡死面（校验组合爆炸、无界队列、close 被锁阻塞、重启队列投毒），以及进程隔离插件在默认无超时策略下的永久挂起风险。
