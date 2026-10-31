# batch-147 — src/features/edit/ai_editing（工具链：时基/估算/审计/loop/facade/invocation）

- 编号区间：AUD-14700–AUD-14799
- 审查日期：2026-09-28
- 文件数：6（2,880 行）
- 汇总：Critical 0 · Major 9 · Suggestion 22

---

## src/features/edit/ai_editing/timebase.py (行数: 331)

### 发现

- [AUD-14700] [Major] 时码路径按名义帧率换算，与秒路径的真实帧率帧号体系不一致
  - 位置: src/features/edit/ai_editing/timebase.py:91-101（对照 :58-61）
  - 证据:
    ```python
    h, m, s, f = (int(p) for p in parts)
    fps_value = fps.to_fps()
    # 非整数 fps（NTSC）的 drop-frame：简化为按名义 fps 计算帧号，
    nominal = round(fps_value)  # 29.97 -> 30
    total = ((h * 60 + m) * 60 + s) * nominal + f
    if is_df and drop_frame:
        # drop-frame 跳帧补偿：每分钟跳 2 帧（除了整 10 分钟），这里只记录
        pass
    ```
    而秒路径用真实帧率：`raw_start = start_seconds * fps_value`（:58）。
  - 影响: 同一墙钟时刻经两个入口得到不同内部帧号：NTSC 29.97 下 `01:00:00:00` → 108000 帧，而 `3600s` → 107892 帧，一小时偏差约 108 帧。§13.2 明确要求覆盖"一小时边界、相邻 clip"场景；时码输入的剪辑点会系统性错位，且 drop-frame 补偿连"记录"都没有做（`pass`）。
  - 建议: 时码→帧换算按真实 fps（`frame = round(tc_seconds * fps_value)`），drop-frame 显示偏移仅在 frames_to_timecode 显示侧补偿；或在返回值中强制携带 warning 标注未补偿。
  - 置信度: 中（可能是有意的名义时码简化，但当前两套帧号并存且无任何对账/警告机制）

- [AUD-14701] [Major] timecode_to_frames 不校验时码字段域，畸形时码静默映射为错误帧号
  - 位置: src/features/edit/ai_editing/timebase.py:87-96
  - 证据:
    ```python
    is_df = ";" in timecode
    parts = timecode.replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"invalid timecode {timecode!r} (expected HH:MM:SS:FF)")
    h, m, s, f = (int(p) for p in parts)
    ```
  - 影响: 外部输入（用户/LLM）如 `"00:00:00:99"` 在 24fps 下静默返回 99 帧（帧字段应 <24）；负数字段（`"-1:0:0:0"`）产生负帧号；`m/s` 超 59 不查。违反 .clinerules"外部数据须验证"，产生难以察觉的错误编辑点。
  - 建议: 校验 `0<=f<nominal`、`0<=m,s<=59`、`h>=0`，越界抛 ValueError。
  - 置信度: 高

- [AUD-14702] [Suggestion] frames_to_timecode 不拒绝负帧输入，产生畸形时码
  - 位置: src/features/edit/ai_editing/timebase.py:106-114
  - 证据:
    ```python
    f = frames % nominal
    total_seconds = frames // nominal
    s = total_seconds % 60
    m = (total_seconds // 60) % 60
    h = total_seconds // 3600
    ```
  - 影响: `frames=-1, fps=24` 输出 `"-1:59:59:23"`（Python 负数取模语义），下游显示/解析混乱。
  - 建议: 入口处 `frames < 0` 抛 ValueError（模块自身契约"所有写入时刻是非负 int 帧号"）。
  - 置信度: 高

- [AUD-14703] [Suggestion] from_audio_sample 用浮点运算，违背类文档"全程有理数/整数运算"承诺
  - 位置: src/features/edit/ai_editing/timebase.py:276-283
  - 证据:
    ```python
    def from_audio_sample(
        self, sample: int, sample_rate: int, media_fps: Rational,
    ) -> int:
        """音频 sample offset → 帧号。``frame = round(sample / sample_rate * media_fps)``。"""
        fps_value = media_fps.to_fps()
        ...
        return round(sample / sample_rate * fps_value)
    ```
  - 影响: 大 sample 数 + NTSC 帧率下引入浮点误差；与 TimebaseService 类 docstring 声明的换算纪律（"全程有理数/整数运算到最后一刻才取整"）不一致。
  - 建议: 复用 `_pts_to_frame_via_timebase` 的整数 num/den 运算：`round(sample * fps.num / (sample_rate * fps.den))`。
  - 置信度: 高（不一致事实确定，实际精度影响小）

- [AUD-14704] [Suggestion] seconds_to_frame_range 纯浮点乘法在整帧边界可能偏 1 帧
  - 位置: src/features/edit/ai_editing/timebase.py:58-64
  - 证据:
    ```python
    raw_start = start_seconds * fps_value
    raw_end = end_seconds * fps_value
    start_frame = math.floor(raw_start)
    end_frame = math.ceil(raw_end)
    ```
  - 影响: 当秒值恰为整帧边界但浮点表示略小于/大于真值时（如 `2.675*40` 类十进制陷阱），floor/ceil 会偏 1 帧；对"相邻 clip"精确对接场景（§13.2）可造成 1 帧缝隙或重叠。
  - 建议: 用 `Fraction(start_seconds).limit_denominator(10**9) * Fraction(fps.num, fps.den)` 做精确 floor/ceil。
  - 置信度: 中

- [AUD-14705] [Suggestion] TimebaseConversionResult.source_range 类型注解为 Any
  - 位置: src/features/edit/ai_editing/timebase.py:187
  - 证据:
    ```python
    source_range: Any  # FrameRange；用 Any 避免 import 循环
    ```
  - 影响: 类型检查失效，调用方失去 FrameRange 的静态保障。
  - 建议: `if TYPE_CHECKING: from ... import FrameRange` + 字符串注解 `"FrameRange | None"`。
  - 置信度: 高

### 通过项
- 线程安全：TimebaseService 无状态，可单例复用（docstring 明示且实现无共享可变状态）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：纯数值转换模块，不涉及
- `_pts_to_frame_via_timebase`（:298-313）纯整数 num/den 运算 + half-away-from-zero 取整，den==0 有防御
- `_is_frame_aligned`（:316-331）ticks_per_frame 整除判定数学正确，除零有防御
- 异常路径：seconds_to_frame_range 对负秒、end<start、非正 fps 均显式 ValueError

---

## src/features/edit/ai_editing/token_estimator.py (行数: 131)

### 发现

- [AUD-14706] [Major] ASCII 空白字符被计入 CJK 桶，英文文本 token 估算系统性偏高约 25–30%
  - 位置: src/features/edit/ai_editing/token_estimator.py:61-75
  - 证据:
    ```python
    for ch in text:
        if _is_cjk(ch):
            cjk_count += 1
        elif ch.isascii() and not ch.isspace():
            latin_count += 1
        else:
            # 其他 unicode（emoji/重音字母）按 CJK 量级估
            cjk_count += 1
    ...
    heuristic = cjk_count / _CJK_CHARS_PER_TOKEN + latin_count / _LATIN_CHARS_PER_TOKEN
    ```
  - 影响: ASCII 空格/换行/制表符落入 `else` 分支按 1.5 char/token 计（应为 latin 量级或忽略）。英文散文约 17% 空格：1000 字符估算 ≈321 而实际 ≈250，偏高约 28%，与第 69 行注释"空白/标点……按 latin 量级粗算"自相矛盾。后果：ContextBundle 预算判定向保守侧失真，过早触发 §7.1 的 60% 结构化压缩，上下文被过度裁剪。
  - 建议: `elif ch.isspace(): continue`（或计入 latin_count）。
  - 置信度: 高

- [AUD-14707] [Suggestion] count_tokens_accurate 内层使用宽泛 `except Exception`
  - 位置: src/features/edit/ai_editing/token_estimator.py:108-112
  - 证据:
    ```python
    try:
        enc = _tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"；tiktoken 下载/IO/内存错误被静默当作"未安装"处理。
  - 建议: 收窄为 `except (KeyError, ValueError, OSError)`。
  - 置信度: 高

- [AUD-14708] [Suggestion] estimate_json_tokens 对极深嵌套结构的 RecursionError 未设防
  - 位置: src/features/edit/ai_editing/token_estimator.py:83-87
  - 证据:
    ```python
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # 不可序列化 → fallback 到 str()
        text = str(data)
    ```
  - 影响: 病态深嵌套 dict/list 触发 RecursionError（不属 TypeError/ValueError），估算入口直接崩溃。
  - 建议: 捕获元组加 `RecursionError`，回退 `repr`。
  - 置信度: 高（行为确定，触发概率低）

### 通过项
- 线程安全：无共享可变状态，纯函数
- GPU/GLSL/HDR/信号/状态提交：不涉及
- 可选依赖隔离：tiktoken try-import 正确，无 tiktoken 时默认路径不受影响
- 向上取整方向正确（保守避免低估）

---

## src/features/edit/ai_editing/tool_call_log.py (行数: 300)

### 发现

- [AUD-14709] [Major] 脱敏仅按敏感 key 黑名单匹配，值中内嵌的绝对路径/secret 原样进入审计日志
  - 位置: src/features/edit/ai_editing/tool_call_log.py:102-111、136-145
  - 证据:
    ```python
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if str(k).lower() in _SENSITIVE else _redact(v))
            for k, v in value.items()
        }
    ```
    以及 summarize_result 直接拼接值：
    ```python
    for k in ("new_revision", "artifact_id", "note", "error"):
        if k in result and result[k] is not None:
            val = str(result[k])
            bits.append(f"{k}={val[:60]}")
    ```
  - 影响: 违反本模块自己的红线（"不记 secret / 绝对路径"）。三种泄漏路径：(1) 键名不在黑名单（`directory`/`out`/`target`/`root`/`folder` 等）；(2) 值本身是路径字符串，如 `{"text": "/Users/testuser/项目/xxx.prproj"}`（search_assets 的 text 参数）；(3) facade 的 `"error": str(exc)[:500]`（tool_facade.py:715）——异常消息（如 FileNotFoundError）常含绝对路径，经 summarize_result 的 `error=` 直接入日志。
  - 建议: 除 key 黑名单外增加值级正则脱敏（`(?:/Users|/home|C:\\\\)[^'"\\s]*`、`sk-[A-Za-z0-9]+` 等模式替换为 `<redacted>`）。
  - 置信度: 中（泄漏是否发生取决于实际 args 形态，但脱敏机制缺口确定存在）

- [AUD-14710] [Major] ToolCallLog 无锁且 docstring 自相矛盾，并发记录时 call_id 重复/记录丢失
  - 位置: src/features/edit/ai_editing/tool_call_log.py:165-187
  - 证据:
    ```python
    class ToolCallLog:
        """append-only 工具调用日志（§4.2 step 1 审计）。

        线程安全（单线程模型下无需锁；多线程由调用方在 invoke 外同步）。...
        """
        def next_call_id(self) -> str:
            """生成下一个 call_id（单调递增，便于排序）。"""
            self._counter += 1
            return f"tc-{self._counter:08d}"
    ```
  - 影响: `self._counter += 1` 是非原子读-改-写；`record` 的 append+pop(0) 组合同理。MCP 网关（ai_editing_mixin.py:654 `McpServer(facade=..., call_log=ToolCallLog())`）的请求线程与 UI/agent loop 并发写入时会产生重复 call_id（审计主键冲突、排序错乱）或交错丢帧。docstring 第一句"线程安全"与括号内"无需锁"直接矛盾，调用方无从遵守。
  - 建议: 内部加一把 `threading.Lock` 保护 counter+append（成本可忽略），或把 docstring 改为明确的"非线程安全"并在 McpServer 侧串行化。
  - 置信度: 中（取决于 McpServer 是否多线程处理请求；锁缺失与文档矛盾为确定事实）

- [AUD-14711] [Suggestion] FIFO 淘汰用 list.pop(0)，饱和后每条记录 O(n) 搬移
  - 位置: src/features/edit/ai_editing/tool_call_log.py:177-182
  - 证据:
    ```python
    def record(self, rec: ToolCallRecord) -> ToolCallRecord:
        """追加一条记录。超过 ``max_entries`` 时丢弃最旧的（FIFO）。"""
        self._records.append(rec)
        if len(self._records) > self._max:
            self._records.pop(0)
    ```
  - 影响: max_entries=10_000 饱和后每次 record 触发 1 万元素 memmove；长时间运行的工具会话中持续浪费。
  - 建议: `collections.deque(maxlen=max_entries)`。
  - 置信度: 高

- [AUD-14712] [Suggestion] _redact 不处理 set 等容器类型，原样 repr 进入摘要
  - 位置: src/features/edit/ai_editing/tool_call_log.py:102-111
  - 证据:
    ```python
    if isinstance(value, dict):
        ...
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v) for v in value)
    return value
    ```
  - 影响: args 含 `{"paths": {"/Users/testuser/a", "/Users/testuser/b"}}` 时 set 未递归，repr 原样写入 args_summary，绕过脱敏。
  - 建议: 增加 `isinstance(value, (set, frozenset))` 分支（转 list 递归）。
  - 置信度: 高

### 通过项
- GPU/GLSL/HDR/信号防递归/状态提交：不涉及（纯内存日志）
- record_invoke 异常路径正确：失败记录后 re-raise 不吞异常（:256-262）
- 计时正确：duration 用 time.monotonic，timestamp 用 time.time
- ToolCallRecord.to_dict 可 json.dumps（纯基础类型）
- succeeded 判定兼顾 ok=False 与异常两条路径

---

## src/features/edit/ai_editing/tool_calling_client.py (行数: 361)

### 发现

- [AUD-14713] [Major] tool result 消息先于 assistant(tool_calls) 消息追加，违反 OpenAI/Anthropic 消息顺序协议
  - 位置: src/features/edit/ai_editing/tool_calling_client.py:221-268
  - 证据:
    ```python
    for tc in response.tool_calls:
        ...
        local_messages.append({
            "role": "tool",
            "tool_call_id": tc.call_id,
            "name": tc.tool_name,
            "content": _tool_result_to_text(result, result_pruner),
        })
    else:
        # 把 assistant 的 tool_calls 也记入 messages（保持对话完整）
        local_messages.append({
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": [
                {"id": tc.call_id, "type": "function",
                 "function": {"name": tc.tool_name, "arguments": dict(tc.arguments)}}
                for tc in response.tool_calls
            ],
        })
    ```
  - 影响: 生成的历史为 `[tool, tool, ..., assistant(tool_calls)]`，而 OpenAI tools API / Anthropic tool_use 协议都要求带 tool_calls 的 assistant 消息先于对应 role=tool 消息；接入真实 provider（注释中明确的后续里程碑）后每个多轮请求都会被 4xx 拒绝。当前 DeterministicToolProvider 不读 messages，测试无法暴露。
  - 建议: 进入 tool 执行前先 append assistant(tool_calls) 消息，再逐个 append tool 结果。
  - 置信度: 高（顺序事实确定；影响在真实 provider 接线后显性化）

- [AUD-14714] [Suggestion] tool 调用预算耗尽时 terminated_reason 误标为 "max_iterations"
  - 位置: src/features/edit/ai_editing/tool_calling_client.py:226-232
  - 证据:
    ```python
    if tool_calls_total >= budget.max_tool_calls:
        terminated = "max_iterations"
        diag_dicts.append(make_diagnostic(
            DiagnosticSeverity.WARNING, TaepDiagnosticCode.QUOTA_EXCEEDED,
            detail=f"tool call budget exhausted (max_tool_calls={budget.max_tool_calls})",
        ).to_dict())
    ```
  - 影响: ToolLoopResult.terminated_reason 的契约值只有 `converged/max_iterations/error/cancelled`，两类超限（轮数 vs 调用数）不可区分；上层重试/审计策略据此误判。
  - 建议: 增加 `"max_tool_calls"` 终止原因并在 docstring 契约中登记。
  - 置信度: 高

- [AUD-14715] [Suggestion] run_tool_loop 未捕获 invoke_fn 异常，工具异常会击穿整个 loop
  - 位置: src/features/edit/ai_editing/tool_calling_client.py:241-248
  - 证据:
    ```python
    result, _rec = record_invoke(
        log, invoke_fn, tc.tool_name, dict(tc.arguments),
        client="tool_calling_loop",
        ...
    )
    ```
    （record_invoke 在 invoke_fn 抛异常时 re-raise，见 tool_call_log.py:256-262）
  - 影响: loop 只对 `complete_with_tools` 设了 try/except（:202-212）；若 invoke_fn 抛出（ToolFacade 内部基本全捕获，但插件分支前代码或自定义 invoke_fn 仍可能抛），异常直接逃逸出 run_tool_loop，丢失 ToolLoopResult、terminated_reason 与部分 tool_results。
  - 建议: 包一层 try/except，转为 `terminated="error"` + SCHEMA_INVALID 诊断后返回已积累的 ToolLoopResult。
  - 置信度: 中

- [AUD-14716] [Suggestion] temperature 硬编码 0.7，未提供配置入口
  - 位置: src/features/edit/ai_editing/tool_calling_client.py:202-205
  - 证据:
    ```python
    response = client.complete_with_tools(
        local_messages, tools, temperature=0.7,
    )
    ```
  - 影响: 规划类工具循环通常需要更低温度；签名上有参数却固定传 0.7。
  - 建议: 作为 run_tool_loop 关键字参数透传。
  - 置信度: 高

- [AUD-14717] [Suggestion] result_pruner 参数类型注解为 Any
  - 位置: src/features/edit/ai_editing/tool_calling_client.py:169、289-297
  - 证据:
    ```python
    result_pruner: Any | None = None,
    ...
    def _tool_result_to_text(result: dict[str, Any], pruner: Any | None = None) -> str:
    ```
  - 影响: 模块内已局部 import ToolResultPruner，却没有用于类型；调用方失去静态检查。
  - 建议: `TYPE_CHECKING` 下导入并以 `"ToolResultPruner | None"` 注解。
  - 置信度: 高

### 通过项
- 取消路径：两级检查点（每轮 model 调用前 + 每个 tool call 前），cancelled 语义完整
- 预算防线：max_iterations/max_tool_calls 双上限，for-else 正确处理 0 轮与耗尽
- 审计集成：每个 tool call 经 record_invoke（脱敏摘要 + 失败也记录）
- approval 注入方向正确：模型 args 中的 token 不被读取，grant 由宿主侧参数透传（且 ApprovalService.consume 有 plan_hash/based_revision scope 校验，见 approval_service.py:91/237）
- GPU/GLSL/HDR/信号防递归/状态提交：不涉及（无状态编排）

---

## src/features/edit/ai_editing/tool_facade.py (行数: 1066)

### 发现

- [AUD-14718] [Major] render_preview（EXPENSIVE）与 request_export（FILE_WRITE）完全没有 approval 门禁
  - 位置: src/features/edit/ai_editing/tool_facade.py:753-756、889-898、900-913
  - 证据:
    ```python
    if tool_name == "topos.render_preview":
        return self._render_preview(args)
    if tool_name == "topos.request_export":
        return self._request_export(args)
    ```
    而 `_render_preview` 不接收也不检查 grant：
    ```python
    def _render_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        timeline_id = args.get("timeline_id") or self._session.get_snapshot().active_timeline
        ...
        result = self._session.request_preview(tid, preset_name=args.get("preset_name", "quick_10s"))
    ```
    对照 `_apply_plan`（:841-851）有 `if approval is None: return PERMISSION_DENIED`。
  - 影响: 项目自身契约（tool_result.py:216-222 `_APPROVAL_REQUIRED_PERMISSIONS` 含 EXPENSIVE 与 FILE_WRITE）和本类 docstring（"权限门禁（write/expensive/file_write 需 approval）"）都要求授权；但 `topos.render_preview` 直接创建渲染 job（edit_api.request_preview 无任何授权校验），`topos.request_export` 直接创建导出 job。且 tool_invocation.py:12-17 明确声明"bridge 不重复实现 approval gate——ToolFacade already enforces it"，即上层依赖本类做唯一防线。模型可在无宿主授权的情况下触发昂贵渲染与导出 preflight/job，违反 §0 红线（授权不来自模型侧的宿主确认流程被绕过）。
  - 建议: 为两个 handler 增加 `grant` 参数与 `_apply_plan` 同款的 PERMISSION_DENIED 早退；request_export 若设计为"提议阶段免授权"，需在工具描述与 docstring 中显式声明确认点后移。
  - 置信度: 高

- [AUD-14719] [Suggestion] invoke 顶层宽泛 `except Exception` 一律映射 SCHEMA_INVALID，掩盖领域错误
  - 位置: src/features/edit/ai_editing/tool_facade.py:774-781
  - 证据:
    ```python
    except Exception as exc:
        return {
            "ok": False,
            "diagnostics": [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                detail=f"tool {tool_name} raised: {exc}",
            ).to_dict()],
        }
    ```
  - 影响: 内部 bug、下游服务故障、渲染线程异常都被标为"schema 无效"，误导模型与 §11.2 相同请求相同 diagnostics 的可追溯验证；违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少区分 `KeyError/TypeError/ValueError`（输入问题→SCHEMA_INVALID）与其余（INTERNAL_ERROR 类诊断码）。
  - 置信度: 高

- [AUD-14720] [Suggestion] _json_safe 每次工具返回都全量 json.dumps 校验一次，大响应双重序列化
  - 位置: src/features/edit/ai_editing/tool_facade.py:783-793
  - 证据:
    ```python
    @staticmethod
    def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
        ...
        safe = to_json_safe(payload)
        # 契约检查：能 json.dumps 才返回（失败时抛出，便于发现未覆盖类型）。
        json.dumps(safe)
        return safe
    ```
  - 影响: full detail 的工程快照可达 MB 级，每次调用序列化两次（校验 + transport 再 dumps），工具循环中累积放大。
  - 建议: 用 `__debug__`/配置开关控制校验，或仅对 sanitize 分支抽样校验。
  - 置信度: 高

- [AUD-14721] [Suggestion] invoke 的 plugin 分支每次重建内置工具名集合
  - 位置: src/features/edit/ai_editing/tool_facade.py:700-701
  - 证据:
    ```python
    if self._plugin_registry is not None:
        local_names = {definition.name for definition in list_tool_definitions()}
    ```
  - 影响: 每次工具调用都重新构造全部 ~20 个 ToolDefinition 实例再建 set（list_tool_definitions 每次新建列表），热路径无谓分配。
  - 建议: 模块级 `_BUILTIN_TOOL_NAMES = frozenset(...)` 常量。
  - 置信度: 高

- [AUD-14722] [Suggestion] validate_edit_plan / get_composite_snapshot 返回未经 sanitize_untrusted 标记
  - 位置: src/features/edit/ai_editing/tool_facade.py:821-833、731-733
  - 证据:
    ```python
    def _validate_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        plan = EditPlan.from_dict(args["plan"])
        preview = self._session.preview(plan)
        return {
            "ok": not preview.has_errors,
            "report": preview.report.to_dict(),
            "diff": preview.diff.to_dict() if preview.diff else None,
        }
    ```
    对照 `_get_project_snapshot`（:728-730）做了 `sanitize_untrusted(data)`。
  - 影响: diff/report 含计划与工程中的资产名/轨道名（可能来自素材文件名，属不可信文本，§12 P6），未打 untrusted 标记直接回给模型，注入标记策略不一致。
  - 建议: 与 snapshot 路径统一，先 sanitize_untrusted 再 _json_safe。
  - 置信度: 中

- [AUD-14723] [Suggestion] _render_preview 忽略 schema 声明的 range 参数；active_timeline 为空串时语义错误
  - 位置: src/features/edit/ai_editing/tool_facade.py:889-897
  - 证据:
    ```python
    def _render_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        timeline_id = args.get("timeline_id") or self._session.get_snapshot().active_timeline
        if timeline_id is None:
            tid = None
        else:
            tid = timeline_id.get("timeline_id") if isinstance(timeline_id, dict) else timeline_id
        if tid is None:
            return {"ok": False, "error": "no active timeline"}
        result = self._session.request_preview(tid, preset_name=args.get("preset_name", "quick_10s"))
    ```
  - 影响: (1) 工具 input_schema 声明了 `range`（:276）但实现从不读取，模型传了也无效（edit_api.request_preview 本身支持 clip_range）；(2) `or` 链导致 `active_timeline == ""` 时 tid="" 而非 None，绕过 no-active-timeline 检查；(3) 取 active_timeline 用整档 get_snapshot()，仅为一个字段。
  - 建议: 透传 `clip_range=args.get("range")`；空串归一化为 None；快照取 minimal 档。
  - 置信度: 高

- [AUD-14724] [Suggestion] search_help / get_render_diagnostics 的 limit 参数未钳制
  - 位置: src/features/edit/ai_editing/tool_facade.py:960-962、1016-1018
  - 证据:
    ```python
    query = str(args.get("query", ""))
    limit = int(args.get("limit", 20))
    entries = self._help_index.search(query, limit=limit)
    ```
  - 影响: 负数/超大 limit 直通底层索引；非数字字符串抛 ValueError 被 AUD-14719 的顶层捕获误标为 SCHEMA_INVALID。
  - 建议: `limit = max(1, min(int(...), 100))` 并捕 ValueError 就地返回参数诊断。
  - 置信度: 高

### 通过项
- 授权伪造防线：args 内 approval_token 被忽略，仅宿主关键字参数生效（:685-695、839-851）
- 禁止能力表：FORBIDDEN_CAPABILITIES 显式拒绝 exec/shell/fs/ui 注入（:458-461、691-692）
- prompt injection 隔离：snapshot/search/analysis/render_diagnostics/MCP 输出均经 sanitize_untrusted + to_json_safe 双层处理
- 未知工具与插件异常 fail-closed（_unknown_tool / PLUGIN_TOOL_EXCEPTION）
- ToolDefinition.__post_init__ 对非法 execution_mode 归一为 EXCLUSIVE（fail-closed）
- execution_mode 判定对 MCP/非 READ/非白名单一律 EXCLUSIVE（fail-closed）
- 线程安全：PARALLEL 白名单仅含无状态/锁保护 sink 的只读工具，session 态工具保持 EXCLUSIVE
- 审计：apply 双路径（plan/composite）均回写 audit_sink 且含 revision/generation/hash

---

## src/features/edit/ai_editing/tool_invocation.py (行数: 691)

### 发现

- [AUD-14725] [Major] result_from_facade 把缺少 "ok" 键的成功返回一律改判为 FAILURE 并丢弃数据
  - 位置: src/features/edit/ai_editing/tool_invocation.py:108-131
  - 证据:
    ```python
    ok = bool(result_dict.get("ok", False))
    diagnostics = _extract_diagnostics(result_dict)
    if ok:
        return ToolResult.success(
            tool_name,
            data=result_dict,
            ...
    error_msg = (
        result_dict.get("error")
        or _first_diagnostic_message(diagnostics)
        or "工具调用失败"
    )
    return ToolResult.failure(...)
    ```
    而 ToolFacade 多个工具的成功返回不含 "ok" 键（tool_facade.py:721 `{"manifest": ...}`、:730/:733 `{"snapshot": ...}`、:740 `{"report": ...}`、:799/:807 `{"segments": ...}`、:811/:819 `{"analysis": ...}`、:914-921 request_export 的 `{"job":..., "preflight":...}`）。
  - 影响: 生产路径（chat_run_driver.py:615、agent_task_executor.py:638 构造本 runner）调用 get_capabilities / get_project_snapshot / search_assets / get_asset_analysis / inspect_subtitle_track 等读工具时，成功结果被报成"工具调用失败"，且 failure 路径不携带 data——payload 整体丢失，模型拿不到快照/搜索结果。request_export 更是"job 已创建（副作用已发生）+ 报告失败"的状态不一致。
  - 建议: 判定改为 `result_dict.get("ok", "diagnostics" not in ...)` 的显式契约——最稳妥是 facade 侧统一所有成功返回都带 `"ok": True`，并在 result_from_facade 对无 ok 无 diagnostics 的返回按成功处理。
  - 置信度: 高

- [AUD-14726] [Major] 超时为事后判定，已成功的副作用被改判为 TOOL_TIMEOUT 失败
  - 位置: src/features/edit/ai_editing/tool_invocation.py:426-440
  - 证据:
    ```python
    result_dict = self._invoke_facade(...)
    elapsed_ms = (self._time() - started) * 1000.0
    result = result_from_facade(tool_name, result_dict, duration_ms=elapsed_ms)
    if (
        timeout_seconds is not None
        and elapsed_ms >= timeout_seconds * 1000.0
        and not result.is_denied
    ):
        result = ToolResult.failure(
            tool_name or "unknown",
            error_message=(
                f"工具执行超过 {timeout_seconds:.1f}s；已等待执行完全退出"
            ),
            error_code="TOOL_TIMEOUT",
            duration_ms=elapsed_ms,
        )
    ```
  - 影响: 工具已正常完成且成功（例如 apply_edit_plan 已提交新 revision、request_export 已建 job）但耗时超过 120s 默认预算时，结果被整体替换为 TOOL_TIMEOUT 失败——副作用已发生而向模型/用户报告失败，诱发重试与重复提交。operation_ledger 的 mark_unknown 只在同 call_id 重试时经 RECONCILE 拦截，换 call_id 的重试（模型常见行为）不受保护。且 ToolFacade.invoke 的 timeout_seconds 参数在函数体内从未使用（内置工具无协作取消），超时纯属事后贴标签。
  - 建议: 仅在结果未知（异常路径）时标 TOOL_TIMEOUT；对已完成的结果保留真实 outcome，超时信息作为附加 diagnostic 携带。
  - 置信度: 中（drain-to-quiescence 是注释声明的设计意图，但"成功改判失败"的不一致风险实在）

- [AUD-14727] [Suggestion] 模块级 tool_execution_mode 与 facade.execution_mode 双实现，易漂移
  - 位置: src/features/edit/ai_editing/tool_invocation.py:161-172（对照 tool_facade.py:635-667）
  - 证据:
    ```python
    def tool_execution_mode(tool_name: str) -> ToolExecutionMode:
        """Return a tool's declared scheduler mode, failing closed to exclusive."""
        defn = find_tool_definition(tool_name)
        if (
            defn is None
            or defn.permission != ToolPermission.READ
            or tool_name.startswith("topos.mcp.")
            or tool_name not in PARALLEL_SAFE_READ_TOOLS
        ):
            return ToolExecutionMode.EXCLUSIVE
        return defn.execution_mode
    ```
  - 影响: 静态版只看 list_tool_definitions，不含 plugin/catalog/MCP 逻辑；runner 用的是 facade 版（:337-349），模块级导出版本一旦被新调用方使用，两套判定将给出不同调度模式（并行安全判定漂移是并发风险）。
  - 建议: 模块级函数委托 facade 实例，或删除导出统一入口。
  - 置信度: 高

- [AUD-14728] [Suggestion] record_skipped 只发 RUN_TOOL_FINISHED，事件可能不成对
  - 位置: src/features/edit/ai_editing/tool_invocation.py:656-663
  - 证据:
    ```python
    call_id = str(call_id or "").strip() or f"call-{uuid.uuid4().hex}"
    result = ToolResult.failure(
        tool_name or "unknown",
        error_code="TOOL_NOT_STARTED",
        error_message=reason,
    )
    self._emit_finished(run_id, tool_name or "unknown", result, call_id=call_id)
    ```
  - 影响: 若调用方未先发 RUN_TOOL_STARTED（函数自身不发），持久化 trace 中出现孤立 finished 事件，破坏"observable pair"契约（模块 docstring 第 1 条）。
  - 建议: docstring 明确前置条件，或在此补发 started。
  - 置信度: 低（取决于上游 admission 层是否已发 started，未在本批次文件中验证）

- [AUD-14729] [Suggestion] _invoke_facade 每次调用 inspect.signature 未缓存；time_fn/cancel_check/approval 注解为 Any
  - 位置: src/features/edit/ai_editing/tool_invocation.py:493-502、317-319、357-359
  - 证据:
    ```python
    invoke = self._facade.invoke
    try:
        parameters = inspect.signature(invoke).parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    ```
  - 影响: 每个工具调用一次签名反射（未按 facade 类型缓存）；`time_fn: Any`、`cancel_check: Any`、`approval: Any` 弱类型削弱静态检查。
  - 建议: 按 `id(invoke)`/类型缓存 signature；补 Callable 类型注解。
  - 置信度: 高

- [AUD-14730] [Suggestion] 非 Mapping 的 args 未经校验直接透传给 facade
  - 位置: src/features/edit/ai_editing/tool_invocation.py:372-378
  - 证据:
    ```python
    context = ToolExecutionContext(
        run_id=run_id,
        tool_name=tool_name,
        args=dict(args or {}) if isinstance(args, Mapping) else (args or {}),
        ...
    ```
  - 影响: `args` 为 list/str 时原样透传（facade 期望 dict，`args or {}` 不修复类型）；且默认 `middlewares=()`（:330）不装 ToolInputValidationMiddleware 时该防线完全不存在——校验中间件默认未启用。
  - 建议: 非 Mapping 直接构造 TOOL_INVALID_ARGUMENTS 失败结果；或将 ToolInputValidationMiddleware 设为默认链首。
  - 置信度: 中

### 通过项
- 事件观测：正常路径 started/finished 成对发射，payload 仅元数据（不含 args/secret）
- 中间件防线：pre 异常 fail-closed；post 不可篡改 tool_name、不可把 FAILURE_LENIED 升级为 success（:597-605）
- 幂等账本：REPLAY/IN_FLIGHT/RECONCILE 三态阻断重复副作用；TOOL_EXCEPTION/TIMEOUT 正确 mark_unknown
- 能力目录准入：未声明工具 TOOL_NOT_DECLARED 阻断（含 MCP escape hatch 的 fail-closed 异常处理）
- DENIED 识别：_PERMISSION_DENIED_CODES 同时覆盖新旧两种码值，与 diagnostics.py:39 的 "TAEP_PERMISSION_DENIED" 匹配
- ToolTimeoutPolicy 构造期校验非正超时
- GPU/GLSL/HDR/信号防递归：不涉及

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 9 | AUD-14700, 14701, 14706, 14709, 14710, 14713, 14718, 14725, 14726 |
| Suggestion | 22 | AUD-14702–14705, 14707–14708, 14711–14712, 14714–14717, 14719–14724, 14727–14730 |
