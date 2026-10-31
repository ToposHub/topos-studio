# Batch 138 审查报告 — src/features/edit/ai_editing（MCP 网关 / LLM planner）

任务书：docs/audit/2026-09-28/assignments/batch-138.md（编号区间 AUD-13800–AUD-13899）
文件数：6，总行数：2,539。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 /
状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批次无 GLSL/HDR/信号/UI 面板代码（纯 AI 编辑服务与 IPC/MCP 传输层），相关维度按不适用处理。

---

## src/features/edit/ai_editing/llm_variant_planner.py (行数: 560)

### 发现

- [AUD-13801] [Major] `_allocate_beat_ranges` 在 target_duration < beat 数时算出非正时长，FrameRange 抛裸 ValueError 而非 LLMPlannerError，且与规则引擎行为不一致
  - 位置: src/features/edit/ai_editing/llm_variant_planner.py:532-545
  - 证据:
    ```python
    if not beats:
        return []
    per = max(1, total // len(beats))
    result: list[tuple[str, FrameRange]] = []
    pos = 0
    for i, beat_id in enumerate(beats):
        dur = per + (total - per * len(beats)) if i == len(beats) - 1 else per
        result.append((beat_id, FrameRange(start=pos, duration=dur)))
    ```
  - 影响: 默认 target_beats 有 3 个 beat。total=2 时最后一个 beat dur = 1+(2-3)=0，total=1 时 = -1；
    `FrameRange.__post_init__`（editorial_models.py:46-47）校验 `duration <= 0` 即抛 ValueError。
    该异常从 `plan()` 直接逃逸，违反本模块“规划失败抛 LLMPlannerError”的契约
    （文档字符串第 13-14 行）；`PlannerInput.target_duration`（variant_planner.py:73）与
    `AssistantConfig.target_duration_frames`（assistant_service.py:90）均无下界校验，用户配出
    ≤2 帧的 brief 即触发。规则引擎版本（variant_planner.py:222 `dur = max(1, int(total * w))`）
    永不产生非正时长，此处注释“与规则引擎策略一致”不成立。AssistantService 会兜底转 FAILED，
    但错误信息变成晦涩的 "FrameRange.duration must be > 0"。
  - 建议: 末 beat 用 `dur = max(1, per + (total - per * len(beats)))`，或在函数入口对
    total ≤ 0 / total < len(beats) 提前抛 `LLMPlannerError`（可读文案），并补齐该边界单测。
  - 置信度: 高（数学路径确定；实际触发依赖极端 target_duration，触发频率中等偏低）

- [AUD-13802] [Suggestion] `ContextService` / `ContextSources` 仅以字符串注解出现，从未导入（也无 TYPE_CHECKING 导入），类型检查与 IDE 解析断链
  - 位置: src/features/edit/ai_editing/llm_variant_planner.py:126-127
  - 证据:
    ```python
    context_service: "ContextService | None" = None,
    context_sources_factory: "Callable[..., ContextSources] | None" = None,
    ```
  - 影响: 两个名字在文件内只出现在引号注解里（实际类型在 context_service.py / context_bundle.py，
    于 `_build_messages_via_context` 内 317-318 行延迟导入）。运行时因 `from __future__ import annotations`
    不会报错，但 mypy/pyright 会报 name-defined 错误，IDE 无法跳转/补全。
  - 建议: 增加 `if TYPE_CHECKING: from ...context_service import ContextService; from ...context_bundle import ContextSources`。
  - 置信度: 高

### 通过项
- 线程安全：实例无可变共享状态（除注入的 service），plan() 局部变量安全。
- 异常路径：LLM 调用失败/JSON 解析失败统一转 `LLMPlannerError`；`validate_planner_output` 已
  保证 variants/decisions 为数组（structured_output.py:60-77），confidence 有 0.0-1.0 clamp（422 行）。
- 输入验证：markdown 代码块剥离、schema_version 校验、evidence 校验
  （validate_evidence_selection + allowed_asset_ids 收口，203-217 行）均到位。
- 状态提交一致性：plan 以 PlanMode.PREVIEW 生成，不落库；持久化经 `on_plan_compiled` 回调由宿主
  （AssistantService._plans.store）完成，本模块不越权写仓库。
- GPU/GLSL/HDR、信号防递归：不涉及。
- 性能：候选描述对 transcript 截断 200 字符（352 行），prompt 体量受控。

---

## src/features/edit/ai_editing/mcp_gateway.py (行数: 504)

### 发现

- [AUD-13810] [Suggestion] `handle_request` 兜底 except 把内部异常类型与消息原样回传外部客户端
  - 位置: src/features/edit/ai_editing/mcp_gateway.py:286-287
  - 证据:
    ```python
    except Exception as exc:  # noqa: BLE001 — JSON-RPC handler 必须返回错误，不崩溃
        return _error(req_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    ```
  - 影响: ToolFacade/内部路径抛出的异常文本（可能含文件路径、内部状态）透给 MCP 客户端
    （模块自身声明"MCP 输出是非可信数据"，反向亦应最小化泄露）。v1 仅本机传输，风险低但与
    mcp_local_ipc.proxy 中“不向客户端披露 socket 路径”的口径不一致。
  - 建议: 返回通用文案（如 "internal error"），详细信息 `logger.exception` 落审计/日志。
  - 置信度: 高（代码事实），影响评估中低。

- [AUD-13811] [Suggestion] `_sessions` 字典无并发保护、无上限与回收策略
  - 位置: src/features/edit/ai_editing/mcp_gateway.py:183, 210, 214
  - 证据:
    ```python
    self._sessions: dict[str, McpSession] = {}
    ...
    self._sessions[sid] = session
    ...
    def close_session(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None
    ```
  - 影响: LocalMcpIpcHost 用自己的 `_server_lock` 串行化了 open/close，但 McpPluginHost/stdio/
    其它嵌入宿主可从别的线程并发调用；GIL 下单条 dict 操作原子，风险低。但只增不删的宿主
    （忘记 close）会让会话与 rate_limiter 无限累积。
  - 建议: 在 McpServer 内部对 `_sessions` 加锁或改用带 TTL/LRU 的会话表；提供 sessions 数量
    监控便于审计。
  - 置信度: 高（事实），低（现实风险）。

### 通过项
- 输入验证：jsonrpc 版本/method/id 类型校验、params 必须为对象（441-445）、tools/call 的 name/
  arguments 类型校验、JSON Schema 参数校验（379-381）、协议版本白名单（261-266, 296-301）。
- 权限收口：动态可见性先经 `list_tools_for`（allowlist ∩ permission ≤ ceiling），
  `permits_tool` 的枚举序比较与 ToolPermission 定义序（tool_facade.py:69-77）一致。
- 审计：tools/call 全部经 `record_invoke`（签名核对 tool_call_log.py:229-240 一致）。
- 异常路径：JSON-RPC handler 兜底不崩溃，`_McpParamError`/`_McpRateLimitError` 映射正确。
- 线程安全（会话内）：每会话独立 SlidingWindowRateLimiter，单连接线程串行访问。
- 状态提交：MCP 层不写工程，write 经 approval grant 透传由下游 consume。

---

## src/features/edit/ai_editing/mcp_local_ipc.py (行数: 368)

### 发现

- [AUD-13820] [Major] `QtMcpRequestDispatcher.dispatch` 超时后请求仍会在 GUI 线程执行：超时≠取消，客户端收到失败响应但操作可能照常发生
  - 位置: src/features/edit/ai_editing/mcp_local_ipc.py:60-66
  - 证据:
    ```python
    def dispatch(self, session: McpSession, request: dict[str, Any]) -> dict[str, Any]:
        event = threading.Event()
        envelope = {"session": session, "request": request, "event": event}
        self._bridge.requested.emit(envelope)
        if not event.wait(self._timeout_seconds):
            return _rpc_error(request.get("id"), -32603, "desktop host did not respond before timeout")
        return dict(envelope.get("response") or _rpc_error(...))
    ```
  - 影响: QueuedConnection 排队的 `_handle` 在超时返回之后仍会被 GUI 线程执行并调用
    `handle_request`（tools/call → ToolFacade.invoke）。客户端已被告知 -32603 失败，但动作
    （PREVIEW 级编译/diff，注入更强 policy 时可达 write）事后仍可能生效——响应语义歧义，
    可能引发客户端重试造成双次副作用。GUI 线程被长任务/模态卡住超过 30s 即触发。退出路径同样
    无排水（drain）：pending envelope 在宿主关闭后执行。
  - 建议: envelope 加 generation/abort 标记，超时时标记 stale 并让 `_handle` 跳过执行；或
    `stop()` 时排水并显式取消 pending 请求。
  - 置信度: 中（竞态确定存在；触发依赖 GUI 线程长时间阻塞）

- [AUD-13821] [Suggestion] 消息大小上限在整行读入内存之后才检查，超长行可先耗尽内存
  - 位置: src/features/edit/ai_editing/mcp_local_ipc.py:217-220
  - 证据:
    ```python
    for raw_line in reader:
        if len(raw_line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
            _write_json(writer, _rpc_error(None, -32600, "request too large"))
            continue
    ```
  - 影响: `conn.makefile("r")` 的迭代按 `\n` 切分且无读取上限，客户端发送一条无换行的超长
    数据（GB 级）会先被完整缓冲，MAX_MESSAGE_BYTES 检查为时已晚——同用户进程可对本进程造成
    内存放大 DoS（`_peer_is_current_user` 已排除其它用户，故降级为 Suggestion）。
    `proxy_stdio_to_local_host`（307-311）与 `LocalMcpIpcClient.exchange` 的 `reader.readline()`
    同模式。
  - 建议: 用带 size 上限的 `readline(MAX_MESSAGE_BYTES + 1)`（或分块 recv）读取，超限即拒。
  - 置信度: 高（事实），低（威胁需同 uid）。

- [AUD-13822] [Suggestion] `default_socket_path` 对已存在目录的 chmod 失败被静默吞掉，端点目录可能非 0700
  - 位置: src/features/edit/ai_editing/mcp_local_ipc.py:69-77
  - 证据:
    ```python
    runtime = Path(tempfile.gettempdir()) / f"topos-mcp-{os.getuid()}"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    return runtime / "desktop.sock"
    ```
  - 影响: /tmp 世界可写，其它用户可预先创建 `/tmp/topos-mcp-<uid>/`（属攻击者）；mkdir
    exist_ok=True 成功、chmod 失败被吞，端点目录非属主所有且权限不受控（残余风险被
    `_peer_is_current_user` + socket 0600 的 bind 后 chmod 化解大半，故仅加固项）。
  - 建议: 校验目录属主（`stat.st_uid == os.geteuid()`）不匹配即拒绝启动；或用 `os.mkdir` 的
    独占创建语义并失败即报错。
  - 置信度: 高（事实），低（现实影响，有 peer-cred 兜底）。

### 通过项
- 线程安全：accept 线程 + 每连接线程模型清晰；open/close_session 由 `_server_lock` 串行；
  worker 线程不触 Qt/OpenGL（Qt 调用经 QueuedConnection 编组回 GUI 线程）。
- 输入验证：peer uid 校验优先于一切处理；JSON 解析失败/非对象/超限均有对应 JSON-RPC 错误码；
  socket 路径超长时用 hash 折叠进短目录（103-105）。
- 资源生命周期：listener/线程在 stop() 中关闭并回收，socket 文件 unlink 有 S_ISSOCK 守卫
  （165, 179-181），异常路径 listener.close()（140-142）。
- 权限边界：默认 policy 封顶 PREVIEW 且本地 IPC 不透传 approval grant（108-116），符合“IPC
  不携带 GUI grant”红线。
- 异常路径：dispatch 内部异常转为 -32603 且只回类型名不回消息文本（49-53），无路径泄露。

---

## src/features/edit/ai_editing/mcp_plugin.py (行数: 216)

### 发现

- [AUD-13830] [Suggestion] `McpPluginHost._servers` 自身无锁且无清理路径，`snapshot()` 迭代存在竞态
  - 位置: src/features/edit/ai_editing/mcp_plugin.py:111, 147-149, 208-213
  - 证据:
    ```python
    self._servers: dict[str, tuple[McpPluginDescriptor, McpServer]] = {}
    ...
    if accepted:
        self._servers[descriptor.plugin_id] = (descriptor, server)
    ...
    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "topos-mcp-plugin-host/1.0",
            "connectors": [descriptor.to_dict() for descriptor, _ in self._servers.values()],
    ```
  - 影响: PluginRegistry 内部有锁，但 host 的 `_servers` 在 register 时无锁写入；若宿主支持
    运行期注册（如插件热加载线程），并发的 `snapshot()` 迭代可能抛 RuntimeError("dictionary
    changed size")。另外 `_servers` 只增不删（registry 拒绝重复 id，不会覆盖，见
    plugin_runtime.py:412，但也没有任何移除 API）。当前典型用法为启动期单线程注册，风险低。
  - 建议: `_servers` 读写加锁或复制后迭代（`tuple(self._servers.values())`）。
  - 置信度: 高（事实），低（现实触发）

### 通过项
- 输入验证：`McpPluginDescriptor.__post_init__` 对 id/名称非空、ttl 正整数、data_policy 枚举、
  列表去重规范化均有校验（47-70）。
- 权限边界：adapter 不复制工具权限/approval 逻辑，调用链仍走 McpServer→ToolFacade；
  inactive server 的 handle_request 返回 -32001 而非误执行（192-201）。
- 非秘密数据：descriptor 仅存非敏感元数据，to_dict 截断 display_name/endpoint 长度（75-76）。
- 生命周期：activate/deactivate/health 委托 registry（其内部有锁与状态机），无资源泄漏点。

---

## src/features/edit/ai_editing/mcp_stdio.py (行数: 177)

### 发现

- [AUD-13840] [Suggestion] `_DemoFacade.list_tools` 两个分支返回同一列表，max_permission 过滤逻辑缺失（死代码）
  - 位置: src/features/edit/ai_editing/mcp_stdio.py:126-129
  - 证据:
    ```python
    def list_tools(self, *, max_permission: ToolPermission = ToolPermission.READ) -> list[ToolDefinition]:
        if max_permission != ToolPermission.READ:
            return list(self._tools)
        return list(self._tools)
    ```
  - 影响: 分支完全相同，按签名意图应是按权限过滤（权限高于 max_permission 的工具不返回）。
    当前 demo 仅有一个 READ 工具，无实际功能影响，但契约上是错误的假实现，一旦往 demo 里加
  高权限工具就会越权暴露。
  - 建议: 按枚举序过滤：`[t for t in self._tools if order.index(t.permission) <= order.index(max_permission)]`，
    或去掉无效分支。
  - 置信度: 高

- [AUD-13841] [Suggestion] 行大小上限在整行读入之后才检查（与 mcp_local_ipc 同模式）
  - 位置: src/features/edit/ai_editing/mcp_stdio.py:81-86
  - 证据:
    ```python
    for raw_line in inp:
        if not raw_line.strip():
            continue
        raw_bytes = raw_line.encode("utf-8", errors="replace")
        if len(raw_bytes) > cfg.max_line_bytes:
            _write_response(out, _error(None, JSONRPC_INVALID_REQUEST, "request too large"))
    ```
  - 影响: `for raw_line in inp` 先完整缓冲一行，1MB 上限检查滞后；stdio 对端是启动本进程的
  宿主（可信度较高），风险低。另外 `raw_line.encode(errors="replace")` 对整行再复制一份，大行
  时瞬时内存翻倍。
  - 建议: 有界读取（如 `io.readline(limit+1)` 循环拼接并提前截断），检查用 `len(raw_line)` 近似即可。
  - 置信度: 高（事实），低（影响）

### 通过项
- 安全默认：feature_flags 显式区分 None 与空映射，`{"mcp_server": False}` 默认关闭（70-77），
  未初始化会话被 tools/call 拒绝（gateway 侧）。
- 异常路径：serve_stdio 的 finally 保证会话关闭；JSON 解析/非对象/超限均回 JSON-RPC 错误码，
  不把异常栈写 stdout。
- 入口约束：main() 无 --demo 且无 --socket 时拒绝启动假 host（163-168），--demo 与 --socket
  互斥校验（158-160）。
- 审计：demo server 也挂 ToolCallLog。

---

## src/features/edit/ai_editing/mcp_vision_gateway.py (行数: 714)

### 发现

- [AUD-13850] [Major] `_ensure_client` 全程不持 `self._lock`：并发首调会重复构建 client，输家的 npx 子进程永不回收；且与 `shutdown()` 存在竞态
  - 位置: src/features/edit/ai_editing/mcp_vision_gateway.py:383-415（对比 354-381 的 `_ensure_http_client`）
  - 证据:
    ```python
    def _ensure_client(self) -> McpOutboundClient | None:
        """懒启动 MCP 子进程 + 客户端；失败记住原因并优雅降级。"""
        if self._failed_reason:
            return None
        if self._client is not None:
            return self._client
        ...
        self._client = client
        return client
    ```
    （同文件 `_ensure_http_client` 则是 `with self._lock:` 全程保护，354-381 行。）
  - 影响: 网关是模块级单例（`get_mcp_vision_gateway`），`invoke` 可从 GUI dispatcher 线程与
    agent/IPC 线程并发进入。两线程同时通过 `self._client is None` 检查 → 各建一套
    registry+McpOutboundClient；后写者覆盖 `self._client`，输家 client 的
    SubprocessStdioTransport 在首次 call_tool 时懒拉起 npx 子进程（注释 412-413 行），此后无人
    持有引用也无人 close —— 孤儿 node 子进程泄漏 + 重复握手开销。另与 `shutdown()`（333-346，
    持锁清空 `self._client`）竞态：shutdown 后 `_ensure_client` 仍可把新 client 写回
    `self._client`，该子进程在 App 退出后不会被关闭（shutdown_mcp_vision_gateway 已把单例置
    None，不会再跑第二次）。
  - 建议: `_ensure_client` 主体置于 `with self._lock`（双重检查），`shutdown` 与 ensure 互斥；
    覆盖旧 client 前先 close。
  - 置信度: 高（锁缺失是确定事实；触发依赖并发首调，频率中）

- [AUD-13851] [Suggestion] `_session_images` / `_server_disabled` 的读写均无锁
  - 位置: src/features/edit/ai_editing/mcp_vision_gateway.py:320-331, 421-426, 445-453
  - 证据:
    ```python
    if path and os.path.isfile(path):
        self._session_images.append(os.path.abspath(path))
        if len(self._session_images) > 8:
            del self._session_images[: len(self._session_images) - 8]
    ...
    if ref.startswith("asset:") and asset_resolver is not None:
        ...
    return self._session_images[-1] if self._session_images else ""
    ```
  - 影响: append+trim 非原子；GUI 线程注册图片与 agent 线程 `resolve_image_ref('latest')`
    并发时，`[-1]` 理论上可读到较旧图片（CPython 单操作原子，最坏为旧值，不崩溃）。
    `set_server_enabled`/`is_server_enabled` 同理为弱一致。属一致性瑕疵而非崩溃风险。
  - 建议: 复用 `self._lock` 或改用 `collections.deque(maxlen=8)` + 局部快照读取。
  - 置信度: 高（事实），低（后果）

- [AUD-13852] [Suggestion] `web_available` / `server_statuses` 每次访问都同步触发 profile 文件 + 凭据存储的磁盘 I/O
  - 位置: src/features/edit/ai_editing/mcp_vision_gateway.py:103-128, 272-277, 294；471-472（tool_definitions 间接触发）
  - 证据:
    ```python
    @property
    def web_available(self) -> bool:
        try:
            return bool(self._api_key_provider())
        except Exception:  # noqa: BLE001
            return False
    ```
    （`default_zhipu_key_provider` 每次执行 `ProfileRegistry().load_from_user_file()` 并遍历
    `HybridCredentialStore` 读 key。）
  - 影响: 属性访问伪装成廉价操作，实际每次都读用户配置文件与凭据文件；偏好页刷新、每次
    tools/list、每次 `tool_definitions()` 都会重复磁盘 I/O，UI 线程调用时有卡顿与多余文件访问。
  - 建议: key 解析结果做短 TTL 缓存或由宿主在配置变更时显式失效。
  - 置信度: 高

- [AUD-13853] [Suggestion] `save_and_register_image` 生成的临时图片文件无任何清理机制
  - 位置: src/features/edit/ai_editing/mcp_vision_gateway.py:428-435
  - 证据:
    ```python
    ext = ".png" if "png" in mime else ".jpg"
    fd, path = tempfile.mkstemp(prefix="topos_mcp_img_", suffix=ext)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    self.register_session_image(path)
    return path
    ```
  - 影响: 抓帧/粘贴每次在系统临时目录落一个 PNG/JPG；`register_session_image` 只保留最近 8 个
    引用但从不删除旧文件，`shutdown()` 也不清理——长会话下临时目录持续增长（含用户画面帧，
    亦有隐私残留）。
  - 建议: shutdown/会话结束时删除已注册文件；或注册表淘汰时同步 unlink（宿主明确要保留的除外）。
  - 置信度: 高

### 通过项
- 优雅降级契约：`invoke` 参数解析段有 try/except（622-652）；下游 `McpOutboundClient.call_tool`
  对 transport 异常/工厂异常均吞掉并返回 `{"ok": False}`（mcp_outbound.py:415-420, 551-562），
  因此 call_tool 抛异常风险已由下游封闭，整体不违背“永不抛异常”承诺。
- 权限/审批收口：所有工具定义均为 ToolPermission.READ；stdio profile `requires_approval_for_writes=True`、
  `data_scope="read_only"`；HTTP 端点强制 https（HttpMcpTransport 校验）；API key 不进 profile/command，
  仅经 env 注入子进程（402 行）。
- 输入验证：image_ref/video_ref 解析后 `os.path.isfile` 双检；web_read 强制 http/https 前缀
  （690-691）；query 截断 200 字符（682）；prompt 空时补默认值（655-657）。
- 单例与退出：`get_mcp_vision_gateway` 双检锁正确；`shutdown_mcp_vision_gateway` 关子进程并置 None。
- 偏好页启停：禁用的 server 在 tool_definitions 与 invoke 两处都拦截（473-474, 603-606），无绕过。
- 状态提交/GPU/GLSL/HDR/信号防递归：不涉及。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-13801, AUD-13820, AUD-13850 |
| Suggestion | 11 | AUD-13802, AUD-13810, AUD-13811, AUD-13821, AUD-13822, AUD-13830, AUD-13840, AUD-13841, AUD-13851, AUD-13852, AUD-13853 |

总计 14 条发现。
