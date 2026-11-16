# Files 插件系统重构计划：Eagle 借鉴与原生交互落地

日期：2026-11-17。适用项目：Topos Studio / Topos Color（PyQt6 + ModernGL + GLSL）。

本计划回应“素材管理重构时一并重构插件系统；值得借鉴的地方写入计划，UI 与交互直接完成”，记录本轮落地情况与后续结构性改造的验收依据。

## 1. 范围、证据与进度口径

证据分三类：**实机观察**指本次在用户已打开的 Eagle 中只读查看；**官方文档**指下列链接明确说明的机制；**Topos 设计**是结合本项目提出的实现建议。不要把 Topos 的权限、事务、沙箱与回滚要求写成 Eagle 已经提供的保证。

本次实机查看了工具栏插件浮层、插件中心分类和列表、插件详情、插件“更多”菜单；没有安装、更新、卸载或启停用户的 Eagle 插件。用户机器上的 Eagle 界面与当前官网文档可能存在版本差异，分别记录。

本轮采用两条工作线：原生 UI 接现有能力直接交付；生命周期、隔离、安装更新、领域服务迁移等结构性工作保留为后续任务。列表里有按钮不等于对应后端已完成。

### 本轮 UI 交付核对表

以下项目已完成代码、交互回归和渲染检查；后续后端任务仍保持未勾选。

- [x] **UI-01**：Files 工具栏和右键菜单提供统一插件入口；快捷启动器与插件中心分层。
- [x] **UI-02**：原生插件中心支持搜索、分类、常用/最近使用及状态过滤；空结果清晰。
- [x] **UI-03**：详情显示名称、版本、作者、能力、兼容性、当前加载状态、下次启动状态和诊断。
- [x] **UI-04**：固定/取消固定、最近使用及 Files 范围的快捷键持久化；冲突与输入焦点处理。
- [x] **UI-05**：仅为已验证的四个现有 `/launch` 路由提供原生打开；明确请求提交与真实任务完成的区别。
- [x] **UI-06**：无原生启动路由的旧 Web 插件提供明确的兼容入口，不盲目构造路由或执行插件 JS 字符串。
- [x] **UI-07**：启停写回真实 FE 配置并提示重启；未热卸载时不显示“已停止运行”。
- [x] **UI-08**：已停用及加载失败的插件仍可查看；偏好设置不再只列成功加载项，不再误开根目录着色器插件文件夹。
- [x] **UI-09**：失败反馈、重复点击控制、关闭时的请求清理、无效配置与兼容分支验证。
- [x] **UI-10**：真实现有插件清单截图与交互回归；用户原始图片、视频不作为写入测试目标。

入口：Files 工具栏“插件”或 `Ctrl+Alt+P` 打开启动器；底部“插件中心”或更多菜单进入管理页。默认先刷新清单，保存过的工具快捷键无需先打开中心即可使用。

实现边界见 [架构说明](/Users/heng/Documents/vscode/topos-studio/docs/architecture/files-plugins-ui.md)，截图、隔离真实窗口测试和 **382 项通过**记录见 [验证记录](/Users/heng/Documents/vscode/topos-studio/docs/validation/files_plugins_20261117/README.md)。一并修复了图片工厂空闲关闭时误用 `QObject.thread()` 导致崩溃的小问题。四个原生启动工具是格式工厂、图片工厂、定时关机和解码跑分；另外六个保留明确兼容入口，未宣称其工具界面已全部重写。

**本轮不计为完成**：第三方市场后端、安装包校验与回滚、热启停/热卸载、通用插件 IPC、第三方进程沙箱、原生 Inspector/格式贡献点运行时、全部旧 Web 工具原生重写。下面均有明确任务和验收。

### 实施状态（2026-11-17 结构化批次，fix-ui 分支）

同日在 UI 批次之上交付的结构化工作。状态口径：**已验证**＝代码+自动化通过；**部分**＝契约/核心已验证但接线未闭环；**未开始**如实留空。详见[架构决策](../architecture/files-plugins-shared-contracts.md)。

| 阶段/任务 | 状态 | 交付与边界 |
| --- | --- | --- |
| PL-01 共享 manifest schema 与校验器 | 已验证 | `src/shared/plugins/manifest.py`：命名空间 id+legacy 映射、host_api 区间、平台/依赖/循环校验、未知贡献类型保留并标记不可用、凭据/用户路径拒绝；`legacy_python_manifest` 仅 AST 提取，发现不执行有写入即失败的验收测试。27 项回归 |
| PL-02 独立安装清单与状态投影 | 部分 | `inventory.py`：installed/desired/runtime/failure_phase/pending_restart 分离、refresh 原子+revision、阻断诊断拒未加载/留已加载、旧目录行适配、第三方默认停用。13 项回归。**插件中心 UI 仍读旧 catalog 投影，未切到 PluginInventory** |
| PL-03 调用上下文与生命周期 | 部分 | `lifecycle.py`：上下文冻结+执行前复核、贡献集全有或全无（半失败逆序回滚）、停用顺序（禁新调用→取消→关窗→解绑→释放）幂等、取消请求与确认分离。14 项回归。**旧插件停用仍是重启制，贡献句柄未挂到旧 Blueprint/JS 注册** |
| PL-04/05 命令/窗口/格式声明 | 部分 | 10 个内建 `topos.plugin.json` 全量声明（4 个启动命令+窗口、6 个兼容/格式贡献），catalog 优先读取 manifest、身份不符忽略；`video_pro/topos_image` 声明 probe/metadata/thumbnail 且不含 playback。7 项回归。**窗口宿主、Inspector 运行时、provider 优先级仲裁未实施** |
| PL-06 权限/信任/隔离/后台作业 | 部分 | broker 契约（BrokerDecision/HostIOResult/Protocol/PathGrant/NetworkGrant）抽取到 `src/shared/plugins/broker.py`，AI 实现复用同一类型（tests/ai 625 通过）；`PluginJobs` 统一取消语义。**Files 插件的实际路径/网络授权接线、隔离 worker、沙箱报告未实施** |
| PL-08 安装/更新/卸载事务 | 已验证（本地包核心） | `installer.py`：staged 解包（防穿越/符号链接/炸弹/条目上限）→ manifest 宿主校验（含拒绝 runAfterInstall 钩子）→ 版本目录+current 指针原子切换 → 指针前失败自动回滚旧版本；卸载拒绝运行中插件，数据删除显式分离。12 项回归。**远程仓库签名索引、更新权限差异展示未实施（PL-09/D）** |
| PL-07 同一 Host API | 部分 | `services.py` 统一 ServiceResult/域 Protocol；`LegacyFilesCommandService` 首个实现接入全部启动入口（prepare 冻结→dispatch），文案与旧行为逐字一致，382 项插件回归通过。**Metadata/Collections/FileTask 等域服务仍走旧 HTTP（NR-06 抽取后换实现不改契约）** |
| PL-09 市场与可发现性 | 未开始 | — |
| PL-10 作者体验 | 部分（自动化） | `pack.py`：一键校验（错误拒绝/警告放行）+ 确定性 zip + sha256 sidecar 校验，产物可直接走 installer。4 项回归。**模板、SDK、调试工具未实施** |
| E 旧宿主退役 | 未开始 | §7 退役门槛全部未满足，`external/file_explorer/` 保留 |

新增回归合计约 **81 项**（tests/plugins 68 + files 侧 13），命令：
`.venv/bin/python -m pytest tests/plugins -q` 与
`.venv/bin/python -m pytest tests/files/test_plugin_launch_service.py tests/files/test_plugin_declarative_manifests.py tests/files/test_files_plugin_catalog.py -q`。

## 2. Eagle 值得借鉴的内容

### 2.1 已观察到的 UI 与交互

| Eagle 实机观察 | 用户收益 | Topos 对应做法 | 边界/验收 |
| --- | --- | --- | --- |
| 工具栏插件按钮打开轻量浮层，底部进入插件中心 | 启动工具不必打开设置大窗 | 原生快捷启动器 + 独立管理中心 | 同一插件身份、状态和启动命令，不能各维护一份列表 |
| 浮层含搜索，以及窗口、格式、检查器、开发入口 | 工具多时仍能定位 | 名称/描述/能力搜索；状态和功能分类 | 键盘可遍历，空结果保留清空搜索入口 |
| 最近使用、图钉、更多菜单 | 高频工具位置稳定 | 用户级固定、最近使用；记录成功受理的打开 | 不把打开详情当作使用，不把失败请求写成成功 |
| 更多菜单显示版本、设置快捷键、卸载 | 管理动作集中 | 详情中集中管理；快捷键冲突可见 | 后端未实现卸载前不放可点击的假卸载按钮 |
| 插件中心左侧功能分类，含待更新数量 | 减少长列表查找 | 原生侧栏分类 + 状态筛选 | 更新数据必须来自真实版本源，不能用本地版本猜“待更新” |
| 列表用图标、名称、短描述、右侧动作 | 信息密度较高，浏览轻松 | 紧凑行式卡片；名称优先、状态独立 | 长名称/中文/高 DPI 不挤掉动作或诊断 |
| 安装、已安装、更新三种列表动作 | 用户能直接识别下一步 | 后续用真实安装状态驱动动作 | 离线/不兼容/失败与等待重启各有独立状态 |
| 详情左侧图标、简介、作者、版本与支持信息；右侧介绍/版本记录 | 在执行动作前看清用途和来源 | 原生列表 + 详情布局；后续加入可信说明和变更记录 | 外部文字安全渲染；不加载任意远程脚本 |
| 创建插件、导入本地项目、API 文档集中在开发入口 | 插件作者有明确起点 | 独立开发入口、脚手架、校验、日志 | 导入开发项目只登记，不因浏览目录就执行代码 |

本轮优先吸收信息布局与操作层次。Eagle 的蓝色、图标资源、品牌文案不应原样搬入 Topos；使用项目已有色板、字体和控件状态。

### 2.2 官方机制与 Topos 的结构性借鉴

Eagle 官方把插件分为窗口、后台服务、格式扩展和检查器四种：分别对应交互工具、常驻工作、缩略图/预览，以及右侧属性扩展。Topos 应把这些转为声明式贡献点，避免每个插件自行插入任意 HTML。[Eagle 插件类型](https://developer.eagle.cool/plugin-api/get-started/plugin-types)

| 官方机制 | Topos 采用内容 | 后续任务 |
| --- | --- | --- |
| manifest 声明身份、版本、平台、CPU 架构、关键词和窗口属性 | 发现阶段就能检索和校验；不用导入 Python 才知道插件叫什么 | PL-01 / PL-02 |
| 窗口入口与尺寸、单实例/多实例等可声明 | 统一窗口宿主，复用/唤起现有窗口，约束最小尺寸与关闭行为 | PL-04 |
| 后台服务独立类型，文档提醒切换素材库时更新上下文 | 服务有启动条件、库作用域、停止与取消；不靠工具窗暗中常驻 | PL-03 / PL-06 |
| 格式扩展把缩略图与预览入口分开声明 | 分类识别、探测、元数据、缩略图、播放、导出分别报告能力 | PL-05 |
| Inspector 可按格式和多选条件显示 | 原生右侧扩展区按选择上下文呈现，旧请求不得覆盖新选择 | PL-05 |
| 插件 API 与外部 Web API 各有入口 | 插件、原生 UI 与自动化调用同一领域服务、同一权限和结果契约 | PL-07 |
| 开发入口、示例、调试说明 | 可运行模板、包校验、诊断与性能工具 | PL-10 |

manifest 与窗口属性参见 [manifest 配置](https://developer.eagle.cool/plugin-api/tutorial/manifest)、[窗口插件](https://developer.eagle.cool/plugin-api/get-started/plugin-types/window)。库切换与后台驻留参见 [后台服务](https://developer.eagle.cool/plugin-api/get-started/plugin-types/service)。格式/属性区参见 [格式扩展](https://developer.eagle.cool/plugin-api/get-started/plugin-types/preview)、[检查器](https://developer.eagle.cool/plugin-api/get-started/plugin-types/inspector)。

Eagle 2026-03-27 的 Build 22 公告说明：停用保留设置、停用项灰显并从运行菜单移除，插件中心根据状态显示动作。Topos 应借鉴其状态可见性；当前旧 FE 仅能持久化“下次启动时禁用”，不能据此宣称已释放资源。[Eagle Build 22](https://cn.eagle.cool/blog/post/eagle4-build22)

Eagle 的官方文档说明 Web/Node.js 技术与原生 API 扩展能力；这不足以证明第三方代码受到操作系统沙箱或细粒度权限隔离。Topos 的进程隔离、文件授权、联网范围与凭据代理是本项目额外设计要求。[Eagle Plugin API 介绍](https://developer.eagle.cool/plugin-api/get-started)

## 3. Topos 现状与必须保留的边界

### 3.1 旧 FE 插件链

已核实入口：

- [旧插件管理器](/Users/heng/Documents/vscode/topos-studio/external/file_explorer/plugin_system.py)：`PluginManager.load_plugins()` 固定扫描 `plugins/*/__init__.py`，可选读取 `config.json`，然后 `importlib.import_module()`、注册 Blueprint、执行 `setup(context)`。
- [FE 启动宿主](/Users/heng/Documents/vscode/topos-studio/external/file_explorer/main.py)：插件运行时由宿主初始化；刷新清单不能再调用初始化去重复注册。
- [原生 Files 宿主](/Users/heng/Documents/vscode/topos-studio/src/app/ui/files_panel.py)、[HTTP transport](/Users/heng/Documents/vscode/topos-studio/src/app/ui/native_files_transport.py)：原生界面目前仍使用旧素材服务与认证请求。
- [偏好设置](/Users/heng/Documents/vscode/topos-studio/src/app/ui/preferences_dialog.py)：旧 `_refresh_plugins()` 只读成功加载列表，因此停用项在重启后消失；打开目录使用的宿主 `plugin_directory` 与 FE 实际插件目录不是一回事。

事实纠正：本工作区实际可加载插件目录为 **10 个**，不是 13 个；现行发现机制也不是 `plugin.json` 注册制。迁移时按静态扫描结果核对，不能以过时注释或预期数量作为验收。

旧实现的结构缺口：

1. `plugins` 只含成功加载项；停用、加载失败和兼容拒绝并非完整安装清单。清单、期望状态、运行状态必须分开。
2. 插件元数据在导入后获取，API 版本也在导入后校验。扫描一个未知插件就可能运行其顶层代码。
3. Blueprint 注册、钩子、JS 扩展、UI 槽和文件处理器没有统一贡献所有权/注销句柄；`setup()` 半失败可留下部分注册。
4. 扩展名处理器后注册覆盖前者；排序仅使结果确定，未提供优先级、冲突说明或用户选择。
5. `PluginContext` 持有 Flask app/socket，注册原始 HTML/JS；`get_config(key)` 虽拒绝空 key，但仍直接读宿主配置键，`set_config(data)` 也没有真正的插件命名空间。不要把注释中的“安全/隔离”当作已实现能力。
6. 部分插件修改 `sys.path`，依赖通用 `plugins`/`utils` 模块名；不适合第三方分发和多宿主共存。
7. 没有通用启停、取消/卸载、安装更新事务；UI 不能通过清空列表或改勾选假装运行时已停止。

上述是后续重构入口；本轮最小适配仅补清单和现有明确能力，不扩张旧插件权限。

### 3.2 已有可复用机制

| 已有模块 | 可复用内容 | 迁移约束 |
| --- | --- | --- |
| [AI 插件 runtime](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_runtime.py) | 不可变 manifest、能力 schema、registry、生命周期、版本与执行策略 | 先评估抽取共享协议；不能让 Files UI 深度依赖 AI 私有控制器 |
| [发现](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_discovery.py) | 发现不执行、安装与启用分离、诊断投影 | 保持原有信任语义，新增 Files 来源适配 |
| [信任](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_trust.py) | 签名验证接口、内容 hash 与作者身份的区分 | 当前 HMAC 实现是受控部署/测试工具，不是公开市场公钥签名方案 |
| [资源 broker](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_broker.py) | 默认拒绝、资源授权与实际 I/O 分离 | 声明策略不是沙箱；读取/写入路径最终由宿主适配器强制校验 |
| [隔离](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_isolation.py)、[沙箱报告](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_sandbox.py) | 可选进程边界、硬取消/超时、诚实报告沙箱能力 | 找到 `sandbox-exec`/`bwrap` 不等于已对进程应用沙箱；required 不可静默降级 |
| [状态](/Users/heng/Documents/vscode/topos-studio/src/features/ai_harness/plugin_status.py) | UI 安全状态快照、有限事件日志、脱敏 | 原生中心只消费投影，不读插件私有对象 |
| [效果目录](/Users/heng/Documents/vscode/topos-studio/src/features/shared/effects/effect_catalog.py)、[效果契约](/Users/heng/Documents/vscode/topos-studio/src/shared/contracts/effect_catalog.py) | 稳定效果身份、参数 schema、预览/导出能力 | Files 只登记“由哪个插件贡献”并委托已有 Effect Catalog，不能建立平行效果目录 |
| [格式注册表](/Users/heng/Documents/vscode/topos-studio/src/shared/media/format_registry.py) | 媒体分类的统一扩展名集合 | 扩展名识别不等于可解码，BRAW/R3D 等 SDK 缺失必须明确报告 |

GLSL `.tglsl`、AI `.tai`、Files 工具和工作流插件可以共享身份、信任、版本、安装状态投影，但保留各域执行后端和参数语义。不能为了统一 UI 把所有插件塞入新的万能执行器。

## 4. 目标架构与最小契约

### 4.1 数据流

```text
静态来源/安装仓库 → manifest 校验 → 信任与兼容检查 → 插件记录
                                                    ↓ 显式启用
原生中心/启动器 ← 状态投影 ← registry + lifecycle → 贡献点注册句柄
       ↓ command_id + InvocationContext              ↓
  同一调用服务 → 授权/预算/取消 → 域服务/隔离 worker → 结果/任务/诊断
       ↑
  Web API / MCP / 自动化（同一权限、同一错误码和变更入口）
```

“来源扫描”不产生运行时副作用；“清单刷新”不启用、不启动服务、不重复注册路由。UI 调用不传任意 Python 模块、JS 函数名或宿主对象。

### 4.2 身份、声明、运行状态

**PL-01 / P1：共享 manifest schema 与校验器。**

- [ ] 使用稳定带命名空间的 `plugin_id`；迁移保留 `legacy_id` 映射。身份不由翻译名称、目录绝对路径或版本号决定。
- [ ] 声明 `schema_version`、插件版本、Host API 兼容范围、Topos 最低版本、平台/架构、作者/支持/许可证、分类/关键词、依赖与可选依赖。
- [ ] 每个 capability 带稳定 id、类型、输入/输出 schema、参数默认值/范围、选择条件、可取消/可重试、运行位置和资源策略。
- [ ] 声明预览、CPU/GPU、导出等后端支持；未支持明确拒绝，不把“能看见”解释为“能无损导出”。
- [ ] 不在 manifest/工程包保存 token、API key、用户绝对路径、授权后的完整敏感配置。插件包不被隐式打入 `.topos`。
- [ ] 兼容扩展未知字段；不识别的贡献类型保留声明并标注不可用，不能悄悄丢弃。schema 迁移有版本、备份与测试。

验收：重复 id、版本非法、未来 API、路径穿越、入口缺失、错误 schema、未知字段、缺依赖/循环依赖均可诊断；发现过程放置顶层会写文件的测试插件，确认从未执行。

**PL-02 / P1：独立安装清单与状态投影。**

- [ ] 区分 installed/discovered、用户期望 enabled、runtime loaded/active、running jobs、failed/rejected、pending restart；不要用单一布尔字段表达所有状态。
- [ ] 状态带 revision 和失败阶段、错误码、可采取动作；诊断长度/数量有上限。
- [ ] 补齐停用、失败、未来 API、目录已移除、更新候选记录；并发刷新不改变选中项和检索条件。
- [ ] 第三方来源默认未启用；本地开发明确标记未验证；已有内建插件的迁移启停状态按旧配置继承。

验收：开启设置前后、重启前后、失败后、断网后同一插件 id 保持可定位；版本变化不丢常用和快捷键。

### 4.3 上下文、生命周期与贡献点

**PL-03 / P1：调用上下文与生命周期。**

- [ ] 不可变 `InvocationContext` 包含 invocation id、command id、library id、location、选中 entry ids/版本、宿主 revision、取消 token、deadline、授权句柄；路径由服务解析，虚拟位置不伪装真实目录。
- [ ] 用户触发时冻结选择，执行前校验有效性；多选不能默默只处理第一项。无选择时的行为由 schema 明确声明。
- [ ] 库切换、关闭窗口/宿主、插件停用、更新、取消任务分别定义策略；旧选择回包不得写入新 Inspector。
- [ ] 激活注册贡献集合；失败回滚全部已注册句柄。停用先禁止新调用，再取消/等待任务、关闭窗、解绑事件、释放句柄；失败进入可诊断状态。
- [ ] Qt UI 由 GUI 线程创建；耗时任务进入既有 worker；OpenGL/ModernGL 仍走已有渲染线程/任务队列，释放走原有延迟回收。
- [ ] 清单刷新、重复激活、重复关窗与 stop 幂等；后台服务每库/全局单实例策略明确。

验收：`setup()` 第三步抛异常无残留菜单/路由/provider；停用后无回调；未结束进程不能显示为“已取消完成”；重复启动不产生双窗口/双后台作业。

**PL-04 / P1：命令、窗口和菜单声明。**

- [ ] command 是唯一启动入口，工具栏、启动器、右键、快捷键与外部自动化仅引用 command id。
- [ ] 声明适用格式、选择数量、只读/写入语义、是否需要目标目录、参数 schema；禁用原因可见，匹配逻辑在服务侧统一。
- [ ] 窗口声明最小/默认尺寸、单实例策略、归属、可取消、关闭与任务后台运行关系；复用窗口时显式更新上下文。
- [ ] 贡献菜单采用结构化数据与注册 token；禁止直接注入 arbitrary HTML/onclick 字符串。旧 Web 容器作为显式兼容类型单独标记。
- [ ] 快捷键命令注册复用宿主机制；保留键、同作用域冲突、隐藏 Files、文本输入焦点、跨平台键名均校验。

验收：同一选择通过四个入口获得相同参数和结果；不适用插件不触发请求；停用/卸载后快捷键和菜单同步移除。

**PL-05 / P1：格式、Inspector、导入导出贡献点。**

- [ ] 格式 provider 分开声明 probe、metadata、thumbnail、viewer/playback、import/export；能力清单报告本机后端/SDK可用性。
- [ ] provider 有 id、插件 owner、优先级、格式/魔数、版本和缓存签名；冲突可解释，可设默认，禁止仅“后注册覆盖”。
- [ ] 错误区分不支持、源损坏、后端缺失、超时/取消与锁定；只在允许的错误类别回退，不无限重试或绕过权限。
- [ ] Inspector 根据 kind/格式/单选或多选条件装配，支持折叠和顺序；慢读取异步、有占位和重试、按 invocation generation 丢弃旧结果。
- [ ] 插件不能直接把原图变更伪装成“预览”；变更复用 Files/项目 command 服务，保留 undo/revision/组织关系与未知 payload。
- [ ] 导出先预览目标、命名、冲突、格式与色彩/HDR政策；默认新文件，覆盖策略由领域服务统一执行。生成结果通过同一导入/索引入口回库。

验收：两个插件命中同格式可见决策；损坏缩略图有回退且不循环；HDR/旋转/SAR/奇数尺寸不被 silently 拉伸或截断；卸载 provider 后不展示过期缓存为可编辑原始内容。

### 4.4 资源、接口与安全边界

**PL-06 / P1：权限、信任、隔离与后台作业。**

- [ ] 按库读取、选中项读取、元数据写入、文件写入、联网域名、启动外部程序、昂贵计算分开授权；权限使用宿主实际强制执行的 broker。
- [ ] 配置按 plugin id 命名空间存储；secret 由宿主凭据服务代理，仅传授权句柄。修复旧 Context 任意 key 读宿主配置的边界。
- [ ] 来源状态与内容摘要分开；第三方签名验证使用公钥/受信任发行者，支持吊销与离线明确策略，不把 hash 当作者认证。
- [ ] 第三方/不可信或重解码任务使用隔离 worker；RPC 输入输出、文件/像素/字节、CPU/内存、并发、超时、进程数有界。
- [ ] 按平台报告实际 isolation/sandbox；要求 OS 沙箱但不可用时拒绝运行并解释，不能静默退为宿主内 Python。
- [ ] background service 声明全局/库作用域、自动启动条件、网络/空闲策略，支持状态/暂停/停止；库切换撤销旧授权和订阅。
- [ ] job 统一 id/进度/阶段/逐项结果/取消/重试；取消是请求，终止确认后才能发布 cancelled。非幂等写入需幂等键或确认计划 id。
- [ ] 日志、崩溃报告与诊断包脱敏，不含 token、完整私人路径、原始图片/音频；联网和上传能力先显示接收方与数据范围。

验收：越权路径、符号链接逃逸、网络重定向、超大输出、死循环、worker crash、停用期间任务、凭据泄漏夹具全部覆盖；插件失败不拖死 Files 浏览和播放。

**PL-07 / P1：同一 Host API，不复制业务。**

- [ ] 先定义域服务契约：query/selection、metadata、collections、thumbnail/preview、safe file tasks、import/export、jobs、events。
- [ ] 原生 UI、插件 SDK、HTTP/Web API、未来 MCP 都是该服务的适配器；锁目录、权限、schema校验、revision、幂等和错误码一致。
- [ ] 统一结构化结果：受理/拒绝、invocation/job id、阶段、逐项结果、warning、可重试与详情；HTTP 200/launch accepted 不代表工具窗口成功显示或素材已处理完。
- [ ] 事件有订阅句柄、版本、顺序与库作用域；快速 selection/theme/progress 事件合并和背压；业务变更不能仅靠前端事件保存在内存。
- [ ] 本地服务认证保留；token 不放URL、日志、插件静态页或任意JS；跨域/重定向/远程host策略独立验证。

验收：同一变更从 native/SDK/API/MCP入口（存在者）得到相同 revision、持久化和组织关系；旧接口暂保兼容并带可测弃用计划。[Eagle Web API 的内外接口区分](https://developer.eagle.cool/web-api)

### 4.5 安装、更新、开发者和生态

**PL-08 / P1→P2：安装/更新/卸载事务。**

- [ ] 安装路径与运行路径隔离；本地包/开发目录/可信远程仓库是不同来源，不隐式互相升级信任。
- [ ] 包内容摘要、签名、schema、版本/平台/架构、入口边界、依赖、许可证、大小预算先验证；拒绝压缩包路径穿越与解压炸弹。
- [ ] staged 解包 → 校验 → 暂存版本 → 用户策略允许启用 → 原子切换；崩溃/磁盘满/不兼容时回滚原版本与配置。
- [ ] 更新前展示变更记录、兼容性、权限差异和是否重启；新增权限不能偷偷继承授权；不自动执行 `runAfterInstall` 式钩子。
- [ ] 卸载先停新任务、处理在途、注销贡献；“移除插件”和“删除插件数据/凭据”分开。用户素材与工程绝不属于插件卸载目录。
- [ ] 支持离线包、版本固定、更新检查策略、失败重试、回滚版本；网络不可用不影响已安装工具检索与运行。

验收：安装/更新每个阶段注入失败，始终能回到旧版可用或明确拒绝状态；重复安装幂等，不能把半包显示为已安装。

**PL-09 / P2：市场与可发现性。**

- [ ] 分类、关键词、适用格式、平台、版本兼容、是否离线/联网、收费/外部账号、来源/验证状态可检索。
- [ ] 列表与详情展示真实来源和状态；介绍/截图/版本记录/支持链接可离线缓存且安全渲染。
- [ ] 下载量/评分只辅助发现，不作为可信度判定；未知统计不显示虚构数字。
- [ ] 推荐不要读取/上传用户素材；若做云端搜索，查询隐私与数据用途明确。
- [ ] 远程索引有 schema/version/签名/缓存过期策略，安装包来源约束与索引来源分别验证。

验收：断网、来源下线、恶意描述、过期索引、未来版本可诊断；已安装页始终可用。

**PL-10 / P2：插件作者体验。**

- [ ] 提供 command/window、background、format、inspector 四类最小模板；Python/native与受控Web宿主各自明确支持范围。
- [ ] 本地开发项目导入只读校验，显式启动；开发模式标记清楚，不能把开发例外带到正式第三方安装。
- [ ] SDK具备类型、API版本、取消/deadline、模拟上下文、受限I/O示例；示例不暴露宿主app或私有配置。
- [ ] 一键校验manifest、能力schema、权限、资源预算、打包内容、入口、兼容范围；确定性构建生成摘要。
- [ ] 按插件查看生命周期日志、调用trace、错误、耗时/峰值内存；可复制脱敏诊断包；资源贡献归属可追踪。
- [ ] 开发工具支持断点/性能分析的方式按真实宿主实现，勿把Eagle F12/DevTools说明直接当PyQt插件既有能力。[Eagle 调试说明](https://developer.eagle.cool/plugin-api/get-started/debugging)

验收：新作者通过模板能完成“选一张图 → 只读分析 → 显示结果 → 取消/关闭”；模板不依赖用户本机绝对路径或私有模块。

## 5. 当前 10 个内建插件逐项迁移

目录均已核实位于 [旧 FE 插件目录](/Users/heng/Documents/vscode/topos-studio/external/file_explorer/plugins/)。下列“目标”是后续设计；当前原生按钮不能冒充全部已迁移。

| 旧 id / 已核实入口 | 现有贡献 | 原生过渡 | 后续目标与专门验收 |
| --- | --- | --- | --- |
| `format_factory` / `__init__.py: launch_gui` | Qt 窗口、菜单、POST `/launch` | 复用路由，JSON `{path: ""}`；单文件输入，多选明确拒绝而非丢选 | window + export command；安全命名/目标/冲突预览，输入快照与执行一致 |
| `image_factory` / `launch_gui` | Qt 图片工厂、POST `/launch` | 复用现有无参数窗口，不能宣称已自动接收全部选中素材 | image batch command；输入数组、输出计划、颜色/HDR与逐项结果 |
| `shutdown_timer` / `launch_gui` | Qt 定时器、托盘、POST `/launch` | 仅打开窗口；启动器不能直接执行关机操作 | system utility window；系统行为需单独明确意图，重复打开不创建多个倒计时 |
| `decode_benchmark` / `launch` | Qt 跑分窗口、JS 菜单与 capabilities | 复用 `/launch` 的 `paths` 数组与 GUI 信号桥 | diagnostic command；来源/时间线上下文、真实CPU/GPU预算与取消；基准测试不改素材 |
| `code_editor` / `setup`、read/save 路由 | Monaco 页面、JS handler、上下文菜单 | 兼容 Web 界面入口，不伪造通用 `/launch` | window/editor provider；保存走安全服务、编码/外部变更与失败恢复 |
| `image_editor` / `setup`、read/save 路由 | Darkroom 页面、图像 handler、JS | 兼容入口与可见状态 | edit command；效果仍委托现有 Effect Catalog；预览/导出一致，改图事务与undo |
| `ai_library_browser` / `setup`、search/status/stats | 旧侧栏、语义检索 JS | 兼容入口，避免出现无响应的原生搜索按钮 | library query provider；复用已有AI服务/凭据/任务，库切换不串结果，离线状态可见 |
| `audio_pro` / `setup` | 音频 handler 与旧JS播放器 | 明确播放由既有播放器/兼容宿主处理 | audio viewer + inspector；复用共享播放/时钟与波形缓存，关闭释放设备和任务 |
| `video_pro` / `setup` | `.topos` 缩略图/元数据 provider、mainwindow_ready hook | 显示其格式/播放能力，不显示“打开工具”假按钮 | format/playback adapter；复用 shared playback 路由，不新增一套视频引擎；嵌入/独立宿主同语义 |
| `topos_image` / `setup` | `.toos` 缩略图/元数据 provider | 格式扩展详情，后端缺失可诊断 | format provider；保留TPIM/TPIC内容识别和惰性native加载，codec缺失不拖垮整个宿主 |

各插件未声明元数据的字段在旧适配中标为缺失或使用可解释的内建映射，不通过导入补齐。后续补 manifest 后迁移到共享 schema，保留旧 id→新 id 的配置和快捷键别名。

## 6. 分期、依赖与交付件

| 阶段 | 工作包 | 交付件 | 完成门槛 |
| --- | --- | --- | --- |
| **本轮（已完成）** | UI-01～10 与最小旧能力适配 | 原生UI、真实清单、四路由打开、启停重启提示、测试/截图 | 382 项回归通过，四个真实窗口通过；不包含新插件runtime完成声明 |
| **A / P1** | PL-01、02、03、07 的契约 | 共享schema/兼容策略、状态模型、调用服务接口、架构决策 | 发现不执行，清单完整，所有入口指向同服务，旧插件不回归 |
| **B / P1** | PL-04、05与10个内建迁移 | owner化贡献句柄、window/command/provider/Inspector adapters | 激活半失败回滚，停用资源收敛；旧Web仍是显式兼容项 |
| **C / P1** | PL-06、08核心 | broker/隔离适配、jobs、可回滚本地包安装 | 实际边界可测；失败/取消/退出不伪成功 |
| **D / P2** | PL-08更新、09、10 | 签名索引、更新/回滚、作者SDK/工具 | 离线可用、版本固定、权限变更可见、模板可独立运行 |
| **E** | 移除旧JS注入与旧FE宿主依赖 | 服务迁移与兼容退役报告 | 满足下节全部退役门槛后才切换 |

每期仅推进一条可验证调用链；不把“接着重构全部后端”作为UI验收前置，也不因界面已改就删除旧可用工具。

## 7. 测试、性能与退役门槛

### 功能与失败矩阵

| 维度 | 必须覆盖 |
| --- | --- |
| 清单 | 启用/停用/失败/未来API、元数据缺失/损坏、目录移除、重复id、未知配置键保留 |
| 生命周期 | 初启/重复启用、半失败回滚、停用时有任务、硬超时、worker crash、关窗与宿主关闭 |
| 上下文 | 无选择/单选/多选/混合格式、目录/搜索/集合、跨库、回包乱序、选择后源被替换 |
| UI | 中英文/长文、高DPI、窄窗、键盘、搜索无结果、固定/最近/快捷键持久化与冲突、真实不可用原因 |
| 变更 | 写入失败、同名冲突、跨卷、磁盘满、DB失败、计划过期、重试、undo/revision与索引关系 |
| 安装 | 每步注入失败、恶意归档/签名错误/依赖循环/权限扩大、断网、回滚、重复安装与卸载 |
| 资源 | 关闭后无UI回调，事件/窗口/任务/provider句柄归零，线程/进程/GPU资源最终收敛 |

写入测试只用隔离临时目录/配置/库；用户提供的照片和视频用于只读预览、选择上下文、格式能力验证。专业编码缺SDK、8K解码超时等保留真实诊断，不能因为插件存在就勾选“支持”。

### 性能预算（建议，需基准后冻结）

- [ ] 记录机器、插件数量、冷/热缓存、数据集与方法，再设置预算；以下是目标不是实测结论。
- [ ] 启动器热打开目标 p95 <100ms，1000条清单搜索目标 p95 <50ms；大清单使用模型/代理过滤，不同步创建1000个重窗口。
- [ ] 发现/签名/依赖/更新检查在后台；GUI回调按批次投影，单批不产生可见掉帧；打开启动器不触发解码与插件启动。
- [ ] Inspector异步任务与浏览缩略图共享总预算；取消过期任务，限制预览总像素/字节，避免每个插件各开满核线程池。
- [ ] 4K播放期间测浏览/搜索/后台分析的帧时与交互延迟；插件不能抢占既有GPU呈现队列。
- [ ] 连续100次启动/关闭、启停、切库后，检查内存、Qt窗口、线程/进程、订阅和GPU句柄是否收敛；正常关闭不在GUI线程等待长任务。

### 旧 Web 插件宿主退役前必须全部满足

- [ ] 10个内建插件的有效能力逐项有新入口或明确保留兼容方案，不能只迁移名称和图标。
- [ ] 通用command、格式、Inspector、后台服务和安装/启停状态可完整表达，原始HTML/JS注入不再是隐藏必要依赖。
- [ ] 原生启动、任务完成/失败/取消、元数据与文件操作经同一服务，稳定id、旧配置、快捷键、用户插件数据可迁移与回滚。
- [ ] 插件安全边界、资源清理和真实媒体矩阵通过，未支持能力在UI明确拒绝。
- [ ] 与 [Files替换后续计划](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-17-files-replacement-followup-plan.md) 的 FUP-01～09 对齐：安全写入/关系传播、上下文/分页、任务取消、播放/GPU边界、服务独立和数据迁移不得被插件层绕过。
- [ ] 独立模式与嵌入模式各通过真实入口验证；移除旧宿主后启动应用不再加载隐形Web页面仅为初始化插件。

## 8. 最终验收记录模板

每个任务完成后填写：实现文件、旧/新接口映射、自动化命令与结果、真实界面截图、异常/取消/关闭验证、未支持能力和回滚方式。状态只能使用“未开始 / 实施中 / 已验证 / 受阻”，没有代码或结果证据时不标“已验证”。

本计划中的官方资料用于解释设计借鉴，仓库与运行时结果才是 Topos 完成状态的依据。后续若引入新插件目录、新 API 或更改兼容策略，必须同步更新本计划、架构文档与测试。
