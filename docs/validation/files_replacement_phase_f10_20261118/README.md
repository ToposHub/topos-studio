# F 阶段第十批（F10）：受限进程与有界 RPC；平台隔离如实报告

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/sandbox.py`
（新）+ 控制器启动门禁接线。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `sandbox.py`（新） | ① **`SandboxCapabilities.probe()`**：平台实际隔离能力如实报告——macOS 探测 `sandbox-exec`（本机在：seatbelt 级），POSIX rlimits，缺失即降级并写明原因（探测注入测试锁定"sandbox-exec 不存在"时的诚实降级）。② **`RestrictedProcessRunner`**：最小环境白名单（macOS 平台注入的 `__CF*` 如实区分）、cwd 收紧、CPU/NOFILE rlimits（不设 RLIMIT_AS——macOS 虚拟布局与进程级 AS 上限不兼容，注释说明）、deadline 终止、有界输出；`require_seatbelt` 且平台不足时**拒绝执行不降级冒充**；seatbelt profile 拒写保护根/拒网络。③ **`BoundedRPC`**：子进程 stdin/stdout 的 JSON 往返——请求/响应字节上限、整体 deadline（**看门狗线程到点杀进程**——阻塞读无法自查预算，修复途中实证）、越界/超时/坏 JSON 都终止并回收（不留挂死通道）。④ **`ExecutionGate`**：可信 builtin 直接放行；第三方必须 seatbelt 级隔离，否则拒绝并明示"未保护的第三方执行入口已禁用"；`plan_isolation` 如实报告能力缺口（"隔离能力不足：第三方执行被禁用，如实报告而非冒充沙箱"） |
| `files_plugins_controller.py` | 启动门禁生产接线：非 builtin 源的启动按**当前启动管线实际隔离**（`LAUNCH_PIPELINE_ISOLATION = "none"`——平台有 sandbox-exec ≠ 本管线用了它）判定——G 阶段受限执行接入前第三方一律拒绝并如实说明 |

## 验证

`tests/plugins/test_sandbox_f10.py`（新，16 项，本机真进程/真 seatbelt）：
- 能力探测结构 + sandbox-exec 缺失时诚实降级（restricted_env + 原因）；
- 最小环境（白名单外不进子进程；平台注入区分）；deadline 杀长任务；
  输出有界；**seatbelt 拒写**（保护根内建文件失败、文件不出现）；
  require_seatbelt 平台不足拒绝执行（"不冒充"）；
- RPC：正常往返；超界响应杀进程并回收；挂死进程到点终止（预算报错）；
  超界请求在出发前拒绝；
- 门禁：builtin 任意隔离放行；第三方 restricted_env/none 拒绝（未保护
  入口禁用文案）；seatbelt 放行；能力缺口如实报告；
- **生产接线**：伪造"可启动但 source=installed"的行 → 启动被门禁拒绝
  （transport 零到达）。

回归：tests/plugins + tests/files + native 插件 UI —— **742 通过**。

## 诚实边界

- **受限执行路径（第三方代码真正跑进 seatbelt 进程）G 阶段接入**——本
  批交付执行边界/门禁/如实报告；当前启动管线实际隔离为 none，第三方
  因此被全部拒绝（正确的保守行为）。
- Seatbelt profile 简版（拒写保护根 + 拒网 + 拒 rm）；细粒度 profile
  随 G 的真实第三方执行需求收紧。
- RLIMIT_AS 未设（macOS 兼容性，注释说明）；CPU/NOFILE 构成有界性。
- sandbox-exec 被苹果标记弃用——能力探测缺失时自动降级并如实标注。

## 隐私

合成子进程/临时目录；无隐私路径。
