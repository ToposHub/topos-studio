# 共享插件契约（Stage A / PL-01·02·03·07）架构决策

日期：2026-11-17。对应[插件系统重构计划](../plans/2026-11-17-files-plugin-system-rebuild-plan.md) A 阶段。
起点 `fix-ui @ 7f025973` 之上的工作区；本文记录跨域插件契约的位置、语义与边界。

## 决策

新建 `src/shared/plugins/`（与 `src/shared/actions/`、`src/shared/diagnostics/` 平级）承载
GLSL/AI/Files 三域共享的插件契约。**本阶段只定义共享层并让 Files 启动路径先接入**，
不搬移 AI 域现有运行时（`src/features/ai_harness/plugin_*.py` 保持原状、其测试不回归）；
AI 域迁移到共享层属于后续阶段，避免一次重构两大域。

| 模块 | 计划项 | 内容 |
| --- | --- | --- |
| `manifest.py` | PL-01 | 带命名空间 `plugin_id`（`<domain>.<name>`）+ `legacy_ids` 映射；schema_version/host_api 区间/Topos 最低版本/平台架构/依赖（必选+可选）；capability 带 kind（command/window/background_service/format_provider/inspector/menu）、输入输出/参数 schema、selection_mode、write_semantics、cancellable/retriable、run_location、resource_policy；窗口经 command 打开；格式 provider 分离 probe/metadata/thumbnail/playback/export。未知贡献类型保留并标记 `available=False`，未知字段原样往返；manifest 内禁止凭据键与用户绝对路径。`validate_manifest_set` 覆盖重复 id、legacy 冲突、宿主兼容、依赖缺失/循环、窗口引用、provider 能力。`legacy_python_manifest` 仅 AST 提取旧 `__init__.py` 常量，**扫描永不执行插件代码**（有写入即失败的验收测试） |
| `inventory.py` | PL-02 | `PluginRecord` 把 install（installed/discovered/missing）、desired_enabled（用户意图）、runtime（not_loaded/loaded/active/failed/rejected）、failure_phase、pending_restart 分开；`PluginInventory.refresh` 原子替换全量记录并递增 revision，旧快照永不出现半刷新；阻断级诊断把未加载记录判 rejected、已加载记录保留可见仅打诊断；记录携带外来 manifest 身份（`RECORD_ID_MISMATCH`）直接拒绝。`legacy_files_records` 把旧 Files 目录行适配进统一状态；第三方来源默认停用（`default_desired_for`） |
| `lifecycle.py` | PL-03 | `InvocationContext` 不可变：调用 id、command id、库/位置、**手势时刻冻结的 entry_paths**（执行前 `selection_valid` 复核，过期要响亮失败）、deadline、CancelToken、授权句柄。`ContributionSet` 激活全有或全无（半失败逆序回滚全部句柄）；`PluginLifecycleController` 停用顺序固定：禁新调用→取消任务→关窗→解绑事件→释放句柄，幂等；`PluginJobs` 取消是请求，worker 确认后才叫 cancelled，未确认的停用报告 `fully_stopped=False`（不谎报） |
| `services.py` | PL-07 | `ServiceResult`/`ItemResult` 统一结构化结果（accepted≠窗口已开；stage/revision/retriable）；`PluginCommandService`（prepare 冻结→dispatch）、Metadata/Collections/Thumbnail/FileTask/Job/Event 各域 Protocol。Qt-free、域中立；域 DTO 绑定放在各 feature 包 |
| `broker.py` | PL-06 | 资源授权契约（BrokerDecision/HostIOResult/PluginResourceBroker/PathGrant/NetworkGrant）从 AI 域抽取共享；AI 的 Allowlist/PolicyBound/PluginHostIO 实现改 import 共享类型并原位 re-export，tests/ai 625 项不回归 |
| `installer.py` | PL-08 | 本地包事务：staged 解包（路径穿越/符号链接/解压炸弹/条目上限全拒）→ 宿主校验（含拒绝 runAfterInstall 钩子）→ `versions/<v>` + `current` 指针原子切换；指针切换前任何失败自动清理半包、旧版本原样保留；同版本幂等、平级/降级默认拒绝；卸载先拒运行中插件，"移除插件"与"删除数据"显式分离 |
| `pack.py` | PL-10 | 作者侧一键校验（错误拒绝构建/警告放行）与确定性打包（同输入同字节 zip + sha256 sidecar），产物可直接进入 installer |

## 首个真实接线

`src/features/files/plugin_launch_service.py::LegacyFilesCommandService` 实现共享
`PluginCommandService`：四个已核实路由（format_factory/image_factory/shutdown_timer/
decode_benchmark）经 `prepare`（复用旧 catalog 的单/多/无选择规则，非字符串选择拒绝而非
静默丢弃）→ `dispatch`（提交 HTTP，受理≠窗口就绪）。`FilesPluginsController` 的全部入口
（启动器/插件中心/快捷键/更多菜单）改走该服务；用户可见文案与旧路径逐字一致，
382 项插件回归不回归。其余六个旧 Web 工具继续走显式兼容入口，不伪造路由。

## 与 ai_harness 的关系（复用而非复制）

共享层吸收了 AI 域已验证的不变量（计划 §3.2）：

- 安装(enabled=False)/启用/激活三级分离；恢复工程状态绝不隐式激活
- 默认拒绝 + fail-closed；观测通道异常被吞（不是执行路径）
- 取消诚实标注（协作/非协作分开）；沙箱报告区分 available 与 enforced
- 事件日志有界（200）并脱敏；manifest 冻结防漂移

AI 域后续把 `plugin_runtime.PluginManifest` 等迁移到共享 manifest（保留 content_hash/
签名字段映射），届时 `tests/ai/` 全量回归为准；本阶段未动 AI 运行时。

## 边界与未做（诚实清单）

- 本阶段**没有**实现：热启停/热卸载（停用仍需重启的旧语义保留）、通用插件 IPC、第三方
  进程沙箱接线、远程市场与签名索引（PL-09）、作者模板/SDK/调试工具（PL-10 剩余）
- `MetadataService` 等域 Protocol 是契约；原生 transport 仍走旧 HTTP（NR-06/FUP-09
  的服务抽取完成后同一 transport 改为实现类，契约不变）
- 停用顺序中的"关窗/解绑"由宿主回调注入；Files 旧插件没有这些钩子，等 B 阶段迁移
- 旧 `FileExplorerPluginCatalog` 仍负责启停写回与清单投影；PL-02 的
  `PluginInventory` 尚未替换插件中心 UI 的数据源（B 阶段接线，先保证契约与语义可测）
- `installer.py` 的信任链只有内容摘要/确定性构建；公开市场所需的公钥签名与吊销
  属 PL-09/D 阶段
- 10 个内建 manifest 声明的窗口/格式贡献是**声明**；窗口宿主与 provider 仲裁运行时
  未实现，UI 不显示假按钮

## 验证

- `tests/plugins/test_shared_manifest.py`（27）、`test_shared_inventory.py`（13）、
  `test_shared_lifecycle.py`（14）、`tests/files/test_plugin_launch_service.py`（8）
- 发现不执行：`test_legacy_python_discovery_never_executes_plugin_code`
- 全部通过后随批次提交；命令：
  `.venv/bin/python -m pytest tests/plugins/test_shared_manifest.py tests/plugins/test_shared_inventory.py tests/plugins/test_shared_lifecycle.py tests/files/test_plugin_launch_service.py -q`
