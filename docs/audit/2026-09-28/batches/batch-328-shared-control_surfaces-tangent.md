# 批次报告：batch-328 — src/shared/control_surfaces/tangent//

编号区间：AUD-32800–AUD-32899。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次为 Tangent 控制面板集成的共享基础层（Qt 客户端 + TIPC/TUBE 线格式 + 偏好常量），不涉及 GPU/GLSL/HDR 数据路径。交叉验证参考（只读）：src/features/color/integrations/tangent_bridge.py、src/features/color/preferences.py。

## src/shared/control_surfaces/tangent/__init__.py (行数: 45)

### 发现
- 无。纯 re-export 模块，无逻辑：导出 `TangentHubClient`、协议编解码函数（`decode_message`/`encode_*`/`frame_message`）、协议数据类型（`TangentCommand`/`PanelInfo`/`PanelType`/两个命令枚举）以及 settings 中的控制面板常量与 `normalize_control_surface`。`__all__` 与导入一一对应，无缺失、无多余。

### 通过项
- 纯导出无逻辑，无线程/GPU/信号/异常路径可审（通过，无逻辑）。
- `__all__` 与实际导入符号一致。

## src/shared/control_surfaces/tangent/hub_client.py (行数: 365)

### 发现
- [AUD-32800] [Major] `sendall` 在 0.25s socket 超时下可能部分发送，返回 False 后连接保持"健康"，导致出站帧流损坏
  - 位置: src/shared/control_surfaces/tangent/hub_client.py:165-175（配合 :187 的 `settimeout(0.25)`）
  - 证据:
    ```python
    def _send(self, message: bytes) -> bool:
        with self._socket_lock:
            current_socket = self._socket
            if current_socket is None:
                return False
            try:
                current_socket.sendall(message)
            except OSError as exc:
                logger.debug("Tangent Hub send failed: %s", exc)
                return False
        return True
    ```
    以及 :187 `current_socket.settimeout(0.25)`。
  - 影响: `socket.timeout` 是 `OSError` 子类（已验证）。当 Hub 进程存活但停止读取（TCP 窗口收缩至 0）时，`sendall` 会在 0.25s 后抛出超时，且按 Python 语义不保证已发送多少字节（部分帧已写入）。`_send` 仅返回 False，不关闭、不标记连接损坏；下一个 `_send` 在被截断的旧帧后追加新帧，Hub 侧读到错位的长度前缀 → 协议流损坏。期间 `connected` 状态仍为真，UI 与面板静默失同步，只有等 Hub 主动断开才触发重连；若 Hub 读到非法帧头后不断开，会话将长期处于坏状态。另外 `_run` 内收到非法帧长（:203）也会按"连接失败"计数，叠加本问题可在 Hub 实际在线时误报 `unavailable` 并退化为 30s 探测。
  - 建议: 捕获发送失败（超时/OSError）后主动关闭 socket 并置 `self._socket = None`，让接收循环退出并走正常 reconnect 路径；或改用非阻塞 select/循环 send 并显式跟踪已写字节数，失败即拆连接。发送失败也应计入 `_report_connection_failure` 而非仅 debug 日志。
  - 置信度: 中（部分发送机制与异常类型已验证；实际触发依赖 Hub 停滞这一边缘场景）

- [AUD-32801] [Suggestion] `stop()` 的有界 join 超时后不检查残留线程，紧接的 `start()` 可被 `is_running` 误判而空跑，集成静默失效
  - 位置: src/shared/control_surfaces/tangent/hub_client.py:131-134（另见 :98-105）
  - 证据:
    ```python
    thread = self._thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    self._thread = None
    ```
    ```python
    def start(self) -> None:
        if self.is_running:
            return
        self._consecutive_failures = 0
        self._reconnect_now.clear()
        self._stop_event.clear()
    ```
  - 影响: worker 在 `create_connection(..., timeout=1.0)`（:183-186）内阻塞时，`join(timeout=1.0)` 与之赛跑可能超时；`stop()` 随即把 `self._thread` 置 None 且不记录告警。若旧 worker 尚在退出途中而用户立即 `start()`，`is_running`（`thread.is_alive()`）可能仍为 True 使 `start()` 提前返回，待旧线程退出后系统中无任何 worker 在运行——表面 start 成功、实际集成死掉。反之，若旧 worker 尚未读到 stop 标志而 `start()` 已 clear 了 `_stop_event`，旧 worker 会复活并可能与新 worker 并存（双连接、信号重复）。
  - 建议: join 后检查 `thread.is_alive()` 并记录告警/重试 join；用一把生命周期锁串行化 start/stop（或循环 join 至线程真正退出再返回）；`start()` 不 clear `_stop_event`，而是通过新建独立的事件/线程代次（generation）避免复活旧 worker。
  - 置信度: 中（竞态窗口为毫秒级，正常路径 join 均可成功；但缺少同步属结构性缺口）

- [AUD-32802] [Suggestion] INITIATE_COMMS 握手发送失败仅记 debug 日志，仍发出 `panels_received` 并继续会话
  - 位置: src/shared/control_surfaces/tangent/hub_client.py:322-333
  - 证据:
    ```python
    if not self._send(
        encode_application_definition(
            self.application_name,
            self.system_path,
            self.user_path,
        )
    ):
        logger.debug("Tangent Hub disconnected before app definition")
    self.panels_received.emit(panels)
    # This causes Hub to request the values for controls it is showing.
    self._send(encode_all_change())
    ```
  - 影响: 若 application definition 发送失败（结合 AUD-32800 的部分发送更可能出现），Hub 从未收到应用定义，不会转发任何参数事件；但客户端仍发 `panels_received`，消费方（tangent_bridge）会认为会话已建立，UI 显示已连接而面板完全无响应，直到 socket 最终断开才自愈。
  - 建议: 握手消息发送失败应视为连接失败：主动关闭 socket 走重连路径（重连后 Hub 会重发 INITIATE_COMMS，握手可重试），而不是继续发 `panels_received`。
  - 置信度: 高（代码路径确定；自愈依赖后续断连，期间行为误导）

### 通过项
- 线程安全（除上述发现外）：`_socket` 的读写均在 `_socket_lock` 内；`_run` 的 finally 以 `if self._socket is current_socket` 防止误清新连接的 socket；跨线程只经 Qt 信号传递数据，worker 线程不触碰 widget/OpenGL（与类 docstring 声明一致）。
- 信号防递归：worker 侧只 emit，不做程序化控件更新；递归防护属消费方（bridge/UI）职责，本层无需状态锁。
- 异常与取消路径：`stop()` 有界（事件唤醒 + shutdown + join），worker 内 `create_connection` 1s 超时、socket 0.25s 超时均为有界等待；`_handle_command` 内的 `return` 处于 try 块中，finally 仍会关闭 socket，无 fd 泄漏；`stop()` 自调用防护（`thread is not threading.current_thread()`）避免自 join 死锁。
- 部分帧解码失败处理正确：长度前缀保证可安全跳过坏帧并 `continue`，不拆会话（:207-215）；非法帧长（流已失步）才拆连接。
- 输入验证：INITIATE_COMMS 载荷做 isinstance 三重校验（:316-321）；参数事件经 `int()`/`float()` 规范化；ns 时间戳用 `object` 信号类型规避 Qt 32 位 int 溢出（:53-57，有注释）。
- 性能模式：退避重连（0.5→5s 指数退避）+ 4 次失败后转 30s 静默探测 + `wake()` 立即恢复，避免重试风暴与日志刷屏；接收缓冲按帧上限（≤1MB）有界分配。
- 类型注解：全文件函数/属性/信号参数均有注解，`from __future__ import annotations` 齐备。

## src/shared/control_surfaces/tangent/protocol.py (行数: 211)

### 发现
- [AUD-32803] [Suggestion] `_pack_f32` 对超出 float32 范围的值抛 `OverflowError` 而非约定的 `ValueError`，会从 `send_parameter_value` 未捕获地传播到调用线程
  - 位置: src/shared/control_surfaces/protocol.py:80-83（触发点 :141 `_pack_f32(value)`）
  - 证据:
    ```python
    def _pack_f32(value: float) -> bytes:
        if not isinstance(value, (int, float)):
            raise ValueError(f"float32 requires a number: {value!r}")
        return struct.pack(">f", float(value))
    ```
  - 影响: 已验证 `struct.pack(">f", 1e300)` 抛出 `OverflowError: float too large to pack with f format`。模块对 uint32/字符串均以 `ValueError` 表达非法输入，唯独 float32 超范围抛 `OverflowError`（`ArithmeticError` 族），既不被 `_send` 的 `except OSError`（:172）捕获，也不符合本模块"数据错误抛 ValueError"的契约。若上层传入未归一的极大值（或被污染的参数缓存），`send_parameter_value` 在调用方线程（UI/bridge）直接抛未捕获异常。当前取色参数均为归一化小量，实际风险低，但属于外部数据验证缺口。
  - 建议: 在 `_pack_f32` 内捕获 `struct.error/OverflowError` 并统一抛 `ValueError`（或对超范围值 clamp 到 float32 最大值并记录告警），与 `_pack_u32` 的防御风格一致。
  - 置信度: 高（机制已验证；实际触发概率低）

- [AUD-32804] [Suggestion] INITIATE_COMMS 解码严格要求 `offset == len(payload)`，与其他命令"容忍尾部扩展字段"的策略不一致，Hub 版本追加字段会使握手永久失败
  - 位置: src/shared/control_surfaces/protocol.py:175-176（对照 :185-188、:200-205 的容忍策略）
  - 证据:
    ```python
    if offset != len(payload):
        raise ValueError("unexpected bytes after Tangent initiate command")
    ```
    而其他命令的注释明确说明容忍尾部元数据：
    ```python
    # Some Hub/Mapper versions append revision-specific metadata to this
    # command. ... so ignore such trailing fields instead of dropping the
    # control-surface link.
    ```
  - 影响: 若某 Hub/Mapper 版本同样向 INITIATE_COMMS 追加修订元数据，解码每次抛 ValueError → hub_client 仅 warning + `error` 信号 + 跳帧（:207-215），应用定义永不发出，集成完全失效且 socket 一直"健康"存活、不会重连自愈。这是本模块自我声明要防御的场景（Hub 版本差异），却在唯一的关键握手命令上未防御。
  - 建议: 与其他命令保持一致——解析完 panel 列表后忽略尾部字节（或仅在 payload 明显短于所需字段时报错），把"尾部多余字节"降为 debug 日志。
  - 置信度: 低（未证实存在会追加字段的 Hub 版本；但策略不一致与失败模式成立）

### 通过项
- 线程安全/GPU 生命周期/信号防递归/状态提交：模块无 Qt、无 socket、无状态（纯函数 + frozen dataclass），全部维度不适用且无共享可变状态。
- 输入验证（除上述发现外）：u32/字符串打包均做类型与范围校验；panel_count > 256 上限防循环 DoS（:168-169）；截断帧由 `_read_u32`/`_read_f32` 的边界检查统一拒绝；`frame_message` 校验 payload 类型与大小上限（≤1MB，防内存放大）；未知命令 ID 容忍为空载荷命令，不拆连接（:207-211 注释明确）。
- 异常路径：所有解码错误均为 `ValueError`（除 AUD-32803），与 hub_client 的捕获策略匹配。
- 性能模式：单帧单命令的简单解析，常量上限、无热路径问题。
- 类型注解：全部函数完整注解，常量用 `Final`，`Final[int]`/`Final[str]` 与 dataclass 字段类型齐备。

## src/shared/control_surfaces/tangent/settings.py (行数: 104)

### 发现
- 无。纯常量表 + 三个纯函数（显示/内部灵敏度换算、控制面板值归一化），无状态、无 I/O、无线程。

### 通过项
- 输入验证：`normalize_control_surface` 对任意 `object` 输入做 isinstance + 白名单校验，非法值回退 `DEFAULT_CONTROL_SURFACE`；换算函数对 TypeError/ValueError 回退默认值。换算函数本身不 clamp 到 `[TANGENT_SENSITIVITY_MIN, TANGENT_SENSITIVITY_MAX]`，但已核实消费侧闭环：偏好页 spinbox 以 MIN/MAX 设定范围（src/features/color/preferences.py:299-305），bridge 读取配置时再做 `max(1.0, min(TANGENT_SENSITIVITY_MAX, ...))` 钳制（src/features/color/integrations/tangent_bridge.py:358-363），直接改配置文件也不会越界生效。
- 类型注解：常量全部 `Final` 带类型，函数签名完整（`object` 入参 + 具体返回类型）。
- 线程安全/异常路径：无可变全局状态、无异常路径（换算异常均被捕获）。
- 常量一致性：`DEFAULT_TANGENT_SENSITIVITY` 的键与 `TANGENT_SENSITIVITY_CONFIG_KEYS` 一一对应且默认值均在 MIN/MAX 区间内；`TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS` 为前者的 hue/sat 子集，语义（trackerball 为二维、brightness 为独立环）有注释支撑。

## 统计

- Critical: 0
- Major: 1（AUD-32800）
- Suggestion: 4（AUD-32801、AUD-32802、AUD-32803、AUD-32804）
