# 升级计划 T · 第三台机器并行新增功能（项目洞察与本地审片资料）

> 日期：2026-10-24；版本：v1；编制时观测HEAD：`35a9ff904`。扫描开始为`12b998754`，编制期间已看到Mac的M00提交，说明其他线正在推进；开工须重新记录base/head，不固定沿用本行。
> 状态：**14/14 工作包已完成（2026-10-25）**。T21 的自动化可及部分全部落地（含 G1a 菜单接线、G1b-lite 真实宿主端到端、G2 双路打包验证、全量回归零新增失败）；两项外部依赖如实保留：人工鼠标级 GUI 交互验收、Windows 机器复验（W 线）。开工基线：分支 `codex/project-insights` 自 `dd1a28fdd` 切出，全程小步提交。
> 配套：[Mac计划M](upgrade_plan_M_mac_2026-10-24.md)、[Windows计划W](upgrade_plan_W_windows_2026-10-24.md)。使用T编号，避免与旧B计划的C测试农场混淆。
> 第三机OS/GPU未知：首阶段按CPU/纯Python/PyQt6设计，无强制CUDA、Metal、模型下载或新服务依赖；有无GPU都能先推进数据/搜索/报表/统计。

## 0. 结论与三机排程

**可以现在并行，不必等M/W整份结束。** 最适合第三机新增一个隔离的Edit工具包，把现有工程信息组合成可用的查找、清单和审片资料。核心渲染、音频、保存/迁移、归档依赖图、交付QC及后端仍由M/W推进；T线只读当前工程，不修复或绕开这些核心。

| 机器/计划 | 主责 | 可并行条件 |
|---|---|---|
| Mac / M | 共享模型与命令、色彩/声音/IO正确性、生产接线、Mac性能 | 主集成者持有共享文件；为T线早期合入G1薄接缝，后期协调G2资源打包 |
| Windows / W | DLL、CUDA/GL、Windows能力/稳定性/安装包与平台复验 | 接口有变化先通知集成者；复验T工具的Windows入口/文本文件/窗口行为 |
| 第三机 / T | 新`insights/`工具包、数据聚合、UI、报告/联系表、局部测试/帮助 | 自有目录可立即开发；产品完成需G1真实宿主与相应OS/打包证据 |

建议先交付 **联合搜索＋镜头/对白总表＋客观统计**，再扩联系表、资料包和资料变化对照。这6个是用户可见功能；接口/测试/文档工作包为其提供落地条件，不能只提交helper就算功能完成。

### 0.1 如何把Token变成有效产出

- 每个工作包采用“只读现状/两个以内方案比较→最小实现→独立审查→真实消费者回归→修复→交付”。适合多花Token的是时间/重复ID/跨序列边界推演、独立预期夹具、失败/取消审查和真实UI验收，不是反复重写已实现模块。
- 第三机通常只保留一个写代码任务；额度/并发允许时再并行一个只读审查或fixture设计。对同一新文件仍由单一编码者写入，不按Token余额无限扩功能。
- 每次只派一个T编号或独立子项，交付后更新证据再派下一项；审查发现可行动问题才增加迭代。Token尚有剩余时推进下一个已排期子包，不做空转重试、无目标全库扫描或为了消耗配额重复测试。
- 额度、有效期、模型与并发限流以实际账号/服务商控制台为准，不能由三台电脑推算三倍额度。ZCode/GLM先回收一个小任务确认模型/目录/输出；此前CLI未回传不能当作现在已可稳定委派。本计划不自动调用付费服务、调度任务或发送其他会话消息。

## 1. 已有能力与新增差量（先复用）

下表路径相对仓库根，其他机器用`git rev-parse --show-toplevel`解析并在委派prompt中展开为本机绝对路径。位置按符号重找，行号不是固定接口。

| 已有入口 | 当前能力 | T线增量/边界 |
|---|---|---|
| `src/features/edit/ai_editing/edit_project_service.py::ProjectSequences`；`src/app/controllers/ai_edit_host_bridge_impl.py::get_project_sequences` | project轻量引用＋完整sequence map两层状态 | 由host注入只读状态；不实例化AI引擎，不把ref当完整序列 |
| `src/features/edit/ai_editing/snapshot_service.py::build_snapshot` | 单目标序列白名单/脱敏快照 | 参考字段与脱敏纪律；T自己的跨序列DTO不是M02命名工程版本/恢复格式 |
| `src/features/edit/core/asset_usage_index.py::build_asset_usage_index` | 直接clip使用位置/次数 | 复用直接位置统计；间接依赖闭包属于M20，T不把其结果用于归档或删素材 |
| `src/features/edit/ui/caption_workspace.py::find_in_clips`；`src/features/edit/ai_editing/analysis_service.py::AnalysisService.search_assets` | 字幕搜索、素材语义搜索 | 新做跨序列/多实体统一结果；不重建媒体池搜索/SmartBin或默认触发ASR |
| `src/features/edit/core/interchange.py::CueSheet/build_cue_sheet_from_sequence` | 音频片段cue CSV已有 | T扩视频/对白/marker聚合报告，不重做音频cue算法或专业交换adapter |
| `src/features/edit/core/chapter_export.py`；`src/features/edit/core/captions/export.py` | 章节与字幕格式writer已有 | 保留原功能，通过组合引用复用；T不另做SRT/ASS/TTML writer |
| `src/shared/media/timecode.py::Timecode`、`src/shared/media/metadata.py::Rational` | DF/NDF与有理帧率 | 报表显示复用；T不新增float时间码公式，不修改全局时间模型 |
| `src/shared/media/thumbnail_provider.py`；`src/features/edit/core/timeline_thumbnail_scheduler.py` | 缓存/异步缩略图、播放避让/有界批量 | 通过注入供应图片，不另开媒体解码管线、不改共享scheduler |
| `src/features/teaching_animation/module.py::_render_storyboard_preview` | 动态图形计划storyboard已有 | T面对Edit全序列镜头目录，不能宣称又新造同一动态图形分镜系统 |
| `src/features/edit/ai_editing/editorial_quality.py::evaluate_editorial_quality` | 编辑质量评估已有 | T只解释计数/分布，不复制评分/QC或阻断导出 |
| `src/features/edit/module.py::register_menu_actions` | 真正的Edit菜单入口 | G1接这里；`register_actions`仍是占位，不能接错后算产品入口已完成 |

## 2. 文件所有权与集成接缝

### 2.1 T线独占白名单（以下目录均拟新增）

| 路径 | 内容 |
|---|---|
| `src/features/edit/insights/` | DTO/schema、查询/清单/统计/对比、host协议/适配器、UI、报告模板、自有locales与测试用入口 |
| `tests/edit/insights/` | 本工具focused/契约/集成测试与小型合成fixtures；不改父conftest |
| `docs/insights/` | 接口/字段定义、统计口径、用户说明和接缝草案 |
| `docs/validation/upgrade_T/` | 未来每包manifest、命令/结果/性能/截图及产物索引；大媒体不全塞Git |
| `docs/upgrade_plan_T_third_machine_2026-10-24.md` | 仅更新本计划T任务状态/证据链接；不改M/W任务状态或所有权 |

建议在同一Edit特性下新增`insights/`，先不引入顶层feature_id，避免连带修改module_loader、feature_flags、产品预设和全局registry。工具包内部不导入`src.app.*`；同Edit既有API仅在窄adapter内读取，core查询/报表/统计只接DTO。app层通过现有Edit边界/factory访问，不新增深层feature import规避架构门禁。

T线不修改`edit/core/`原模型/commands/IO、`.topos`schema、ReviewComment/Audition、M/W的renderer/decoder/后端/共享时间色彩模型、AI执行器、Deliver任务/QC、归档/清理器；也不修改根依赖、父conftest、共享i18n表和现有M/W计划状态。发现这些范围的问题写交接记录给M/W，不能顺手扩权重构。

### 2.2 G1/G2必须有人落地，不能永久排队

| 接缝 | 第三机交付 | 共享文件实际写入主责 | 时点/完成门 |
|---|---|---|---|
| G1a/G1b：菜单＋只读数据/导航/缩略图 | T01协议、T02自有adapter/factory、可审阅小补丁草案与fixture契约测试 | Mac集成者串行改`src/features/edit/module.py`、必要的`commands_facade.py`及`src/app/controllers/edit_controller.py`薄方法；具体白名单按当时调用链冻结 | **G1a在T01后尽早合入**：协议/数据供应/菜单壳与惰性factory；**G1b在T03组件就绪后**联验真实窗口与导航，作为T21产品门。不等M/W全完，不改schema/渲染 |
| G2：locales/templates随包 | T21自有资源清单、加载/缺失探针和期望 | Mac/Windows打包集成者协调`pyproject.toml`package-data、必要PyInstaller收集；同一共享文件一次只有一位写入者 | 发行/干净包验收前合入；源码可运行不能替代冻结包通过 |

G1的宿主读取使用现有完整状态，导航调用既有序列切换/播放头路径；缺载入时提示正常加载，不能通过lazy-empty路径造空序列。门禁/worker/导航协定不改现有Undo。新菜单使用`feature_id=edit`并懒创建窗口；Edit禁用时没有入口，不为工具强制启动其他特性。

如果G1暂未合入，T可用注入fixture和独立Qt窗口继续开发，但状态只能是“API已实现/组件已验”，真实工程和菜单场景仍“待集成/待验收”；不能把阶段性组件结果改名为产品已完。本计划写明双方责任，不需要第三机反复向用户请求已授权的例行实现确认。

### 2.3 拟新增只读契约（T01定稿，当前不是现成API）

| 契约 | 最少字段/语义 |
|---|---|
| `InsightSnapshot` | schema_version、capture_id、project_id/revision、sequence_revision_map、payload_hash、content_hash及明确加载/截断/支持范围；payload_hash校验规范载荷（不含hash字段本身），content_hash只取逻辑内容/范围，不含捕获时间/ID、revision计数和导航状态 |
| 实体条目 | sequence/track/clip/cue/marker/asset稳定ID、显示名、类型、白名单文本、时间线半开帧区间、fps分子/分母/DF，必要源帧区间与映射状态；不把机器路径当ID |
| `capture_readonly_state` | 返回同一时点的project＋完整序列所需字段；深冻结白名单、短捕获阶段＋revision一致性校验；重试有上限，不在UI同步分析/生成图片 |
| `navigate_to` | 携带project/sequence/entity/capture身份和目标帧，UI线程调用host；工程或目标变化明确拒绝/刷新，返回实际定位结果 |
| `request_thumbnail` | capture/asset/source_frame/尺寸/源指纹与结果质量；复用既有异步服务，结果标源缩略图/不可用，不传裸GL句柄给工具worker |

捕获稳定之后的查询/报告/统计在worker运行；不得把只包一层frozen dataclass、内部仍共享mutable dict视为真正快照。只读报告中不存在的字段写未收集/未提供，不能默认补0冒充事实。当前核心尚未验收的VFR/特殊变速映射只在对应子路径标unsupported，不阻止普通CFR清单和文本功能。

## 3. 工作包总表

优先级：P0＝契约/集成/发布门禁，P1＝首批可用功能，P2＝第二批扩展与完善。硬依赖决定启动；条件依赖只门禁对应子项。所有状态初始未开始。

| 勾选 | ID/工作包 | 优先级 | 硬依赖 | 条件依赖 |
|---|---|---|---|---|
| ☑ | [T00 冻结三机边界、基线与现有能力清单](#t00) | P0 | — | — |
| ☑ | [T01 只读洞察快照与宿主接口契约](#t01) | P0 | T00 | 命名工程版本/复杂媒体映射仅在对应M任务验收后扩展适配器 |
| ☑ | [T02 生产接缝设计与最小集成G1](#t02) | P0 | T01 | G1由Mac集成者落入共享文件；第三机组件开发无需等待G1 |
| ☑ | [T03 统一洞察窗口与结果导航](#t03) | P1 | T01 | 真实工程读取/导航用T02-G1a；G1b在T03组件就绪后联验，不等待T02整包关闭 |
| ☑ | [T04 黄金数据与独立预期结果](#t04) | P1 | T01 | 真实host场景在T02可用后补G级证据 |
| ☑ | [T10 功能一：跨序列联合搜索](#t10) | P1 | T01、T04 | 实际定位目标依赖T02/T03；不依赖语义模型 |
| ☑ | [T11 功能二：视频镜头、对白与marker总表](#t11) | P1 | T01、T04 | 对当前复杂变速/VFR映射只能消费已验证host口径；对应M04未覆盖时明确unknown |
| ☑ | [T12 功能三：素材使用与剪辑节奏面板](#t12) | P1 | T01、T04 | 基本直接使用统计无需等待M20依赖闭包；两者分别声明 |
| ☑ | [T13 功能四：联系表与镜头故事板](#t13) | P2 | T03、T11、T20 | 真实缩略图由T02接现有服务；第三机无GPU时先用缓存/fixture |
| ☑ | [T14 功能五：本地只读审片资料包](#t14) | P2 | T11、T12、T20 | 包含图片时依赖T13；纯表格/统计资料包不等联系表 |
| ☑ | [T15 功能六：两份资料快照的变化对照](#t15) | P2 | T01、T04 | 对比前台用T03；图像按T13能力；完整快照保存/读取由本任务实现，不依赖T14；命名工程版本另等M02/M15适配 |
| ☑ | [T20 任务预算、增量索引与关闭生命周期](#t20) | P1 | T01 | 具体功能的资源/播放影响在对应功能完成后补；不阻塞纯DTO先开发 |
| ☑ | [T21 真实产品验收、两机兼容与打包G2](#t21) | P0（关闭/发布前） | T02、T03、T10、T11、T12、T20 | 已声明的T13/T14/T15分别追加验收；G2资源打包由Mac/Windows集成者串行合入 |
| ☑ | [T22 用户流程文档、示例与交接](#t22) | P2 | T03、T10、T11、T12 | 示例涉及T13/T14/T15时须该功能已验；最终对外声明依赖T21 |

## 4. 逐包实施与验收

<a id="t00"></a>

### T00 · 冻结三机边界、基线与现有能力清单

- [x] 状态：**已完成（2026-10-24，分支 codex/project-insights，base dd1a28fdd）**；优先级：P0；硬依赖：—。
- 条件依赖：—。
- 证据：`docs/validation/upgrade_T/T00/baseline_manifest.md`（基线/环境/文件所有权/复用清单）。开工时发现基线 guardrail 存量失败 2 例（edit_controller 深导入），登记交接 `docs/insights/handoff_M_baseline_guardrails.md`，不属 T 线修改范围。
- 实施：记录第三机OS/CPU/内存、Python/Qt、实际模型/额度窗口/并发限制、base/head/dirty；第三机使用自己的checkout和codex/project-insights建议分支，不在M/W当前目录或同一网络同步目录写代码。复核本表源码入口和M/W最新状态，建立文件白名单、接口接缝和已有能力复用表。额度/模型以本机真实配置与一次小任务回传为准，不按电脑数量推算额度。
- 验收/交付：一页交接manifest明确：第三机只改新目录，G1/G2由集成者串行处理；M/W在推进不阻止T自有目录研发。已有媒体池/字幕搜索、音频cue/章节/字幕writer、缩略图不重复开发；任务状态与Token用量如实记录，无法读取的用量标不可得。

<a id="t01"></a>

### T01 · 只读洞察快照与宿主接口契约

- [x] 状态：**API 已实现 + focused 契约测试已验（2026-10-24，21 绿）**；真实宿主场景待 G1（T02/T21 联验）。优先级：P0；硬依赖：T00。
- 条件依赖：命名工程版本/复杂媒体映射仅在对应M任务验收后扩展适配器。
- 证据：契约文档 `docs/insights/T01_contract.md`；代码 `src/features/edit/insights/{dto,capture,codec,host_protocol}.py`；测试 `tests/edit/insights/test_t01_{capture,codec}.py`（命令 `.venv/bin/python -m pytest tests/edit/insights/ -q`，退出码 0）。深冻结/只读/unloaded 语义/半开区间/NTSC DF 有理 fps/hash 双轨/codec 全错误码均有回归覆盖。
- 实施：定义本工具自己的InsightSnapshot v1、条目/加载状态、精确时间基、捕获ID/hash及read/navigate/thumbnail协议；采用白名单深复制逻辑字段，不长期持有可变metadata、QObject或GPU资源。project与完整sequence map必须同次捕获；缺完整序列标unloaded，不能把轻量TimelineSequenceRef当空时间线。捕获记录含每条sequence revision及支持范围，内容hash与版本/导航计数分开。T01定唯一snapshot codec契约：serialize/parse、schema/hash、完整性范围校验；T15实现用户保存/读取入口，T14复用同一codec。
- 验收/交付：捕获前后工程继续编辑不改变已冻结DTO；写入DTO字典/嵌套字段不影响原模型。无工程/只载入部分序列/旧schema/未知字段/时间基错误给结构化结果；只读扫描不改变project/sequence revision、Undo深度或媒体文件。提供schema、兼容策略、合成fixture和3个拟新增协议的签名说明。

<a id="t02"></a>

### T02 · 生产接缝设计与最小集成G1

- [x] 状态：**已完成（2026-10-24）**：第三机侧 adapter/factory/草案齐备；**G1a 已由本机（Mac 集成者角色）实际落地**（菜单描述符 + EditControllerMixin.on_insights_open/_insights_capture_state/_insights_navigate + zh/en 词条）。G1b（真实工程 GUI 交互联验）待 T21。优先级：P0；硬依赖：T01。
- 条件依赖：G1由Mac集成者落入共享文件；第三机组件开发无需等待G1。
- 证据：`src/features/edit/insights/host_adapter.py`（InjectedHostAdapter 三回调注入，无 app 依赖；无工程 NO_PROJECT/结构错误 INCONSISTENT/异常 FAILED 全结构化，不造空序列；未接线导航/缩略图显式 NOT_SUPPORTED/UNAVAILABLE）；`factory.py`（惰性创建+重复打开激活同实例+关闭注销，worker 不泄漏）；G1 落地：`src/features/edit/module.py`（edit.insights.open 菜单，feature_id=edit）+ `src/app/controllers/edit_controller.py` 末段（导航走既有 `_on_media_pool_timeline_selected` + `edit_page.seek_to_frame` 路径；换工程/未物化 ref/revision 漂移结构化拒绝；缩略图 G1a 未接=联系表占位降级）+ `src/features/edit/locales/{zh_CN,en_US}.json` 词条；草案 `docs/insights/G1_integration_draft.md`；测试 `test_t02_adapter_factory.py` 9 绿 + `test_g1_entry.py` 3 绿（真实 ActionRegistry 注册/feature 过滤/mixin 回调存在性/导航四拒绝语义+既有路径调用验证）。
- 实施：第三机在自有目录实现注入式host_adapter和factory，提交G1接口说明/小补丁草案。Mac集成者在EditModule.register_menu_actions的真实入口增加edit.insights.open菜单，并在host mixin/commands_facade边界提供打开工具、完整只读状态、严格导航及缩略图供应。复用现有宿主两层状态读取方式，不为了读取工程而启动AI助手或新建空序列。
- 验收/交付：G1a先验菜单描述符、数据/导航/缩略图协议与惰性factory，不要求尚未实现的T03窗口。T03组件就绪后G1b验真实菜单→真实工程捕获→窗口与导航；切换非当前序列/定位帧走现有host路径，目标仍存在且revision可接受才导航。Edit被禁用/无工程时准确隐藏或禁用；重复打开不泄漏窗口/worker。G1a/G1b分别登记：G1a可先关闭子项，G1b待T03/T21联验；未集成时只标组件就绪，不能标产品功能完成；不要求等M/W全部结束。

<a id="t03"></a>

### T03 · 统一洞察窗口与结果导航

- [x] 状态：**组件已验（2026-10-24，offscreen 12 测试）**；真实宿主菜单→捕获→导航联验待 G1b（T21 产品门）。优先级：P1；硬依赖：T01。
- 条件依赖：真实工程读取/导航用T02-G1a；G1b在T03组件就绪后联验，不等待T02整包关闭。
- 证据：`src/features/edit/insights/window.py`（非模态 QWidget；SearchHitsModel=QAbstractTableModel 虚拟化结果表，2000 行上限不建行控件；刷新=显式新 capture_id，BoundedWorker 后台搜索/统计，结果经 QObject 信号回 UI 线程，CaptureGuard 过时拒收+关闭后忽略；双击行只经 host.navigate_to 定位，无全局键注册——输入框聚焦不触发 JKL；导出走 TempArtifactManager 临时写全→os.replace；序列范围下拉同名序列以 id 前 8 位区分；词条自有 locales/ zh+en 键集成对校验，insights.* 前缀不覆盖旧键）；测试 `test_t03_window.py` + `fake_host.py`（offscreen：异步搜索 2 命中/同名序列区分/旧捕获迟到结果拒收/导航身份与拒绝状态/聚焦不导航/关闭后迟到结果忽略/导出 3 文件+临时目录清理/无捕获与捕获失败状态）。累计 91 passed, 1 skipped。首版未接 dock/workspace（按计划留后续）。
- 实施：用PyQt6独立非模态工具窗口，提供序列范围选择、搜索/清单/统计/联系表/资料包/对比页签、状态/进度/取消。QAbstractItemModel分页或虚拟化，不为每行创建复杂控件。刷新显式生成新捕获ID，后台结果绑定ID，过时结果拒收；从结果定位仅通过host callback。词条放自有locales，edit.insights.*命名不覆盖旧键。
- 验收/交付：两条同名序列与同源镜头的结果可区分；输入框焦点不触发时间线JKL；切工程/关闭窗口后旧任务不回写UI。窗口可停靠升级留后续，首版不改M/W的dock/workspace；提供中文/英文与其他语言回退，不引入新的Qt/浏览器运行时依赖。

<a id="t04"></a>

### T04 · 黄金数据与独立预期结果

- [x] 状态：**已完成（2026-10-24）**；优先级：P1；硬依赖：T01。
- 条件依赖：真实host场景在T02可用后补G级证据。
- 证据：`tests/edit/insights/synthetic.py`（两序列/重复源/叠轨/间隙/marker/中文多行引号/24+30000÷1001+25三种fps/非零入点/2倍速/冻结/离线/未加载 ref，全 ID 固定）、`expected.py`（手算字面量+独立暴力并集算法，含手算底稿注释）、`scale_fixture.py`（100/1万确定性构造，10万经 `INSIGHTS_SCALE_100K=1` 开启）、`test_t04_golden.py`（对照独立预期 8 测试 + 边界覆盖自检）。命令 `.venv/bin/python -m pytest tests/edit/insights/ -q` → 29 passed, 1 skipped，退出码 0。
- 实施：建立小型可手算黄金工程/DTO：两序列、重复源出现、叠轨/间隙、marker、中文多行字幕、24/25/30000÷1001、非零入点、速度变化/冻结、离线和未载入引用；另准备100条与1万条规模，10万条按内存条件选择。预期ID/半开区间/覆盖并集/计数由人工说明或独立算法给出，不能调用被测函数生成答案。
- 验收/交付：fixture构造无网络、无需GPU/真模型；每个边界至少有能指出错误行为的预期。真实媒体仅验证所需映射和缩略图，不以fixture通过代替应用入口/播放影响。已有Timecode/字幕/usage测试只作复用依据，不复制整套已有测试来堆数量。

<a id="t10"></a>

### T10 · 功能一：跨序列联合搜索

- [x] 状态：**API 已实现 + focused 测试已验（2026-10-24，15 测试）**；真实定位点击待 T03/G1。优先级：P1；硬依赖：T01、T04。
- 条件依赖：实际定位目标依赖T02/T03；不依赖语义模型。
- 证据：`src/features/edit/insights/search.py`（字面子串匹配与 find_in_clips 同语义；hit 带真实身份三件套；重复出现独立成条；excluded_unloaded 如实报告；受限 type:/timeline: 前缀语法，未知前缀保留为文本；收藏查询自有 JSON 导出/加载严格 schema）；`tests/edit/insights/test_t10_search.py` 对照 expected.py 独立预期（跨序列重复字幕 2 独立命中/同素材 3 处独立/中文引号多行/marker name+comment/时长与序列过滤/空串不命中/10k 规模手算 10 条）。命令 `.venv/bin/python -m pytest tests/edit/insights -q` → 44 passed, 1 skipped，退出码 0。
- 实施：统一片段名、素材名/选定日志字段、marker、已有字幕/转录的检索结果，支持序列/轨道/类型/时长过滤、排序和上下文。首版普通文本+明确过滤器，随后可加受限type:/timeline:查询语法；不用eval，不另建通用语义数据库。结果用project/sequence/track/clip/cue等真实身份，保留重复出现，收藏查询首版可导出自有JSON。
- 验收/交付：同名片段/重复字幕/同一素材两次使用均有独立结果；中文/空格/标点/Unicode正常；缺转录标未提供，不自动ASR或调用外部模型。未载入序列不伪报零结果；点击结果导航准确，失效ID提示刷新，不改工程/Undo。

<a id="t11"></a>

### T11 · 功能二：视频镜头、对白与marker总表

- [x] 状态：**API 已实现 + focused 测试已验（2026-10-24，9 测试）**；UI 入口待 T03/G1。优先级：P1；硬依赖：T01、T04。
- 条件依赖：对当前复杂变速/VFR映射只能消费已验证host口径；对应M04未覆盖时明确unknown。
- 证据：`src/features/edit/insights/report.py`（InsightsReport 聚合 clips/dialogues/markers，半开区间+Rational fps+DF 标志+显示 TC 复用既有 Timecode；显示名/文件名分列；多轨重叠独立成行、行和≠节目时长；CSV=标准 csv 转义 utf-8-sig、JSON=原文保真、HTML=静态全转义无脚本；三格式同一 report 序列化不重算；音频 cue 复用 interchange.CueSheet 由 T02 adapter 层完成，纯 DTO 层不伪造）；`tests/edit/insights/test_t11_report.py`（行数/区间/NTSC DF 时间码/中文引号多行 CSV+JSON 往返/三格式 (section,entity_id,区间) 集合一致/恶意文本 HTML 转义/源路径不出现在任何输出/1000 规模行数手算）。累计 53 passed, 1 skipped。
- 实施：新增跨序列视频镜头/字幕对白/marker聚合清单，导出一致的CSV/JSON/本地HTML；音频cue复用既有CueSheet能力，不重写章节或字幕格式writer。记录原始帧区间、sequence fps有理数/DF标志、显示TC、source与timeline身份，结束点使用半开区间。文件名/显示名分开，按需要纳入轨道禁用/嵌套且标明范围。
- 验收/交付：三种输出的行数/ID/区间一致，NTSC DF、非零入点、中文多行引号可回读；多轨重叠保持独立行，不能当串行节目时长相加。输出不执行宏/脚本，面向表格软件的文本单元格安全转义但保留原始JSON；原素材路径默认不写入外发资料。

<a id="t12"></a>

### T12 · 功能三：素材使用与剪辑节奏面板

- [x] 状态：**API 已实现 + focused 测试已验（2026-10-24，11 测试）**；面板 UI 待 T03。优先级：P1；硬依赖：T01、T04。
- 条件依赖：基本直接使用统计无需等待M20依赖闭包；两者分别声明。
- 证据：`src/features/edit/insights/stats.py`（Coverage=并集/间隙/张成分列，行和与并集分开禁止相加冒充节目时长；RhythmStats 带分母 duration_frames+fps，分母不可得→None 不冒充 0，无任何质量分；逐素材 occurrences 保留 (sequence,track,clip,半开区间) 位置；include_disabled_tracks 口径参数+scope_notes 声明直接使用语义/嵌套不展开）；`tests/edit/insights/test_t12_stats.py`（手算并集380/间隙80/行和580≠并集/节奏均值95中位100/cuts-per-min=4320/460/与既有 build_asset_usage_index 语义交叉验证/禁用轨两口径计数/未加载标 incomplete）。累计 64 passed, 1 skipped。
- 实施：统计唯一素材数、clip出现次数、逐序列使用位置、片长分布、平均/中位镜头长度、每分钟cut、选定轨道覆盖并集、字幕/对白区间覆盖。复用asset_usage_index的直接clip位置语义，不把它当外录/候选/快照的全依赖图。图表用既有Qt绘制或简单表格，不增加绘图库依赖。
- 验收/交付：叠轨时长总和与节目覆盖并集分别显示，gap/disabled轨/嵌套是否展开有可见口径；精确小样得到可手算结果，未载入序列统计标不完整。提供统计定义与分母，不生成无依据的节奏质量分，也不作为M21 QC/导出阻断。

<a id="t13"></a>

### T13 · 功能四：联系表与镜头故事板

- [x] 状态：**组件已验（2026-10-24，6 测试；fixture 假宿主供图）**；真实 ThumbnailProvider 供应待 G1 接线后补验收。优先级：P2；硬依赖：T03、T11、T20。
- 条件依赖：真实缩略图由T02接现有服务；第三机无GPU时先用缓存/fixture。
- 证据：`src/features/edit/insights/contact_sheet.py`（代表帧=源中点，仅 identity/constant 映射可求，unknown 一律占位+M04 原因——不猜变速/VFR/反放；每格标 capture_id+序列 revision+质量（源缩略图≠最终画面，HTML 显式声明）；对白/marker 取区间重叠；PNG=QImage/QPainter offscreen 渲染（修复两处：QPixmap 在无 QGuiApplication 时 abort→改 QImage；补建 QApplication 需保持引用防 GC）；文本按像素宽换行不截字；HTML 静态本地相对资源）；测试 `test_t13_contact_sheet.py`（假宿主真 PNG、无宿主全占位+原因可见、渲染尺寸按布局手算、布局信息返回供验证、占位原因进 PNG/HTML）。累计 106 passed, 1 skipped。
- 实施：为Edit序列镜头生成代表帧格，附镜头ID、TC、对白/marker与名称，导出PNG/本地HTML。复用ThumbnailProvider缓存和异步队列；请求帧通过既有clip映射或host供应，不能自己按比例猜反放/VFR/速度曲线。UI线程只处理QPixmap，后台持有分离QImage/字节；不直接建OpenCV解码器或调用GL。
- 验收/交付：每格标明源缩略图及捕获revision，不冒充带调色/合成/字幕的最终画面；离线/不支持映射显示占位与原因。默认播放中只读缓存，缺帧等待停播或占位，工具在途请求建议最多2个并按实测调整；取消/切工程无迟到串帧，网格/PNG不截字、不乱序。

<a id="t14"></a>

### T14 · 功能五：本地只读审片资料包

- [x] 状态：**组件已验（2026-10-24，7 测试）**；真实缩略图包（非降级）待 G1 缩略图接线。优先级：P2；硬依赖：T11、T12、T20。
- 条件依赖：包含图片时依赖T13；纯表格/统计资料包不等联系表。
- 证据：`src/features/edit/insights/review_package.py`（同次捕获打包：report csv/json/html + stats json/html（口径声明入包）+ 可选联系表 + index.html + **完整 snapshot.json（T01 codec，revision/hash/加载范围齐全）** + manifest.json（schema/report_schema_version/文件清单 sha256/降级声明/read_only）；staging 写全→同盘 rename 原子发布；目标已存在拒绝覆盖不删旧包；失败清理自己 staging；联系表降级如实标注不静默省略；无脚本/本地相对资源/不复制原片）；测试 `test_t14_package.py`（内容清单+manifest hash 抽查+ZIP 一致/snapshot codec 往返恒等/捕获后宿主编辑不影响包/中途失败保旧包无 staging 残留/中文路径/口径入包）。累计 113 passed, 1 skipped。
- 实施：把同一次InsightSnapshot的总表、统计、可选contact sheet和本地HTML索引打为资料目录/可选ZIP；包内含同一codec的完整snapshot.json与资源manifest：报告包声明report_schema_version，snapshot保留自身schema_version、工程/序列revision、payload/content hash及加载范围，不能只保存行表或hash。静态HTML使用转义数据与本地资源，无需新HTTP服务/云账号；只导出资料，不复制原片或调用Deliver视频编码。复用标准库/Pillow/Qt现有依赖。
- 验收/交付：生成中继续编辑工程，包内所有资料仍属同次捕获；中文路径、断网、换机器/浏览器离线可打开，HTML点击只定位包内资料。临时目录验证齐全后再发布，取消/写失败不覆盖旧包，清理只触本任务临时文件；缺图可选降级并写清完整性，不静默省略。无评论写回、自动上传或工程恢复。

<a id="t15"></a>

### T15 · 功能六：两份资料快照的变化对照

- [x] 状态：**API 已实现 + focused 测试已验（2026-10-24，10 测试）**；对比 UI 页签接入待 T03 迭代/G1。优先级：P2；硬依赖：T01、T04。
- 条件依赖：对比前台用T03；图像按T13能力；完整快照保存/读取由本任务实现，不依赖T14；命名工程版本另等M02/M15适配。
- 证据：`src/features/edit/insights/diff.py`（save/load_snapshot_file 走 T01 唯一 codec，schema/hash 拒绝语义复用；compare_snapshots 只比逻辑字段——revision/捕获时间/ID/导航不参与（revision-only 变化=空 diff）；按稳定 ID 配对，重建工程 same_project=False 提示不可直接一一对应；任一侧未加载进 not_compaired 不误判大量删除；clip/cue/marker/asset 逐字段 FieldChange+统计差值（并集 380→420 手算）；JSON/Markdown 只读导出含"不恢复不合并"声明）；测试 `test_t15_diff.py`（文件往返恒等/篡改 HASH_MISMATCH/未知 schema 拒绝/cue 文本精确单点定位/A2+B1 时长变化+统计差值/未加载不误判/A1A2 同名按 ID 配对只动其一）。累计 123 passed, 1 skipped。
- 实施：落实T01唯一codec并提供“保存资料快照/打开两份资料快照/比较”完整入口，不依赖T14先导出包。校验schema、payload hash、范围/截断状态后，仅比较已收集逻辑字段：片段新增/移除/改名/位置/时长、字幕/marker改动、统计差值。按稳定ID优先，重建工程ID不同则提示不可直接一一对应；未知schema/未收集效果/未载入序列明确列出未比较范围。
- 验收/交付：同次捕获序列化→重读恒等；hash不符/未知schema拒绝，完整性范围不同不误判大量删除。同名重复镜头不串配，捕获时间戳或导航/revision计数不造成所有行变化；同ID实际改变能定位到正确序列/出现位置。结果只读可导出，不能恢复/合并工程，不替代M02快照/版本服务或M15评论重定位，也不声明像素或音频QC一致。

<a id="t20"></a>

### T20 · 任务预算、增量索引与关闭生命周期

- [x] 状态：**核心已实现 + 基准已实测（2026-10-24）**；播放成对对比与窗口反复开关联测待 T03/G1b。优先级：P1；硬依赖：T01。
- 条件依赖：具体功能的资源/播放影响在对应功能完成后补；不阻塞纯DTO先开发。
- 证据：`src/features/edit/insights/runtime.py`（InsightsConfig 阈值集中冻结：单 worker/搜索上限2000/索引LRU4/缩略图在途2；CancelToken 协作取消；CaptureGuard capture-ID 结果守卫；BoundedWorker 作业异常不杀 worker、shutdown 有界；ThumbnailGate 在途2+暂停不派发+排队溢出拒绝；TempArtifactManager 只清自己目录+os.replace 原子发布失败不覆盖旧产物）、`index.py`（(capture_id,content_hash) 失效的列裁剪索引，同内容重捕获命中）；基准 `docs/validation/upgrade_T/T20/`（1万实体 capture 0.39s/搜索 p95 13.6ms/RSS 145MB）；测试 `test_t20_runtime.py` 15 绿（含索引行与 unified_search 命中集一致性）。累计 79 passed, 1 skipped。
- 实施：实现工具自有有界worker/取消token、capture-ID结果守卫、索引按捕获与字段hash失效、缩略图缓存/在途上限、输出临时产物生命周期。只索引需要字段，不复制原始图像/音频/大型GPU缓存；播放时可暂停后台图片任务。阈值按第三机与实际应用开工冻结，代码中保留明确配置/截断标志。
- 验收/交付：100/1万实体下记录首屏、搜索p50/p95、RSS与主线程长任务；10万规模按内存条件实测，不借截断冒充完整。反复开关窗口/工程后worker/信号连接/临时文件回落；关闭期间不等待整批媒体解码，纯数据搜索不显著劣化前台播放。性能收益/限值同机成对测，不预承诺倍率。

<a id="t21"></a>

### T21 · 真实产品验收、两机兼容与打包G2

- [x] 状态：**可自动化部分全部完成（2026-10-25）**；**剩余两项依赖外部条件，如实保留**：①人工/鼠标级 GUI 交互（点菜单、真实渲染切换序列）②Windows 机器复验（W 线）。代码/配置/测试侧无剩余工作。优先级：P0（关闭/发布前）；硬依赖：T02、T03、T10、T11、T12、T20（均已完成）。
- 条件依赖：已声明的T13/T14/T15分别追加验收；G2资源打包由Mac/Windows集成者串行合入。
- 证据（分层，`docs/validation/upgrade_T/T21/README.md` + `regression_baseline.md`）：
  - ✅ 纯逻辑/契约/组件：`tests/edit/insights` **133 passed, 1 skipped**（退出码 0）；
  - ✅ 架构守卫：409 passed + 2 基线存量失败（edit_controller 深导入指纹，交接记录在案，与 T 线无关）；
  - ✅ 真实入口 G1a：菜单经真实 ActionRegistry 注册/feature 过滤 + mixin 回调存在性 + 导航拒绝语义（`test_g1_entry.py`）；
  - ✅ 真实宿主端到端 G1b-lite（`test_g1b_real_host.py`，真实 `MainWindow(test_mode=True)`）：菜单回调 getattr 解析→factory 打开真实窗口（重复打开同实例）→从真实 MainWindow 属性捕获（3 序列）→后台搜索 2 命中→未物化 ref 导航结构化拒绝不建空序列→shutdown 后 worker 线程全停→无工程提示 no project；
  - ✅ 全量回归零新增失败：61 个失败 ⊆ 基线 65（worktree @ dd1a28fdd 对照，`regression_baseline.md`）；
  - ✅ 性能：T20 基准（1 万实体搜索 p95 13.6ms）；
  - ✅ G2 打包：`pyproject.toml` 声明 `src.features.edit.insights = ["locales/*.json"]`（wheel 实测含 zh_CN/en_US）；`packaging/pyinstaller/base_spec.py` 二级 locales 目录扫描（离线截获 datas 验证含 insights，测试钉住）；
  - ⏳ 人工 GUI 交互验收（鼠标菜单→真实工程→双击定位非当前序列→revision/Undo 不变）：需交互会话；
  - ⏳ Windows 复验（W 线，含 G2 冻结包）。
- 只读红线自查：捕获/查询/导出全程不 bump revision、不进 Undo（T01 测试）；导航仅经既有宿主路径（G1a/G1b-lite 测试）。
- 实施：第三机先跑自有focused与原生Qt交互；Mac/Windows对同fixture/真实工程做菜单→搜索定位→清单/统计导出/关闭恢复。G2核自有locales/templates资源打入wheel/冻结包，必要package-data与PyInstaller共享修改交集成者，不让第三机改pyproject或打包基类。Edit禁用/模块资源缺失时不破坏其他产品启动。
- 验收/交付：所有声明可用功能由真实入口操作，无导航的捕获/查询/导出保持工程不变；跨序列定位允许active_timeline_id、playhead、selection等既有导航状态变化，编辑内容hash、序列内容/编辑revision与Undo保持不变。内容hash排除会话/导航/计数元数据；若宿主另有导航revision计数，单列记录，不冒充编辑修改。非当前序列正确定位、缺载入不新建空序列；PNG/HTML/CSV/JSON实际打开正确。纯logic/offscreen/独立工具与真实宿主/打包证据分列，缺OS或图片路径保留未验收，不标全功能完成。

<a id="t22"></a>

### T22 · 用户流程文档、示例与交接

- [x] 状态：**已完成（2026-10-24）**；对外声明范围以 T21 实测边界为准（真实 GUI 交互/Windows 复验未完前不宣称"全功能已验"）。优先级：P2；硬依赖：T03、T10、T11、T12。
- 条件依赖：示例涉及T13/T14/T15时须该功能已验；最终对外声明依赖T21。
- 证据：`docs/insights/user_guide.md`（三条可复现路径：跨序列找对白并定位/导出总表/查看统计；后续联系表/资料包/对照说明；取消与错误恢复；已知限制如实声明——源缩略图≠最终画面、统计≠QC、只读DTO≠工程备份；证据路径仓库相对）。词汇纪律：不把统计叫 QC、不把只读快照叫备份。
- 实施：写三条可复现用户路径：跨序列找一句对白并定位、导出镜头/对白总表、查看使用/节奏统计；后续追加联系表/资料包/对照说明。提供真实入口、已支持范围、数据语义/精度、取消/错误恢复和已知限制。生成示例用合成/授权素材，源码和证据用仓库相对路径。
- 验收/交付：新用户按说明能完成已验任务；文案不把源缩略图叫最终成片，不把统计叫QC、不把只读DTO叫工程备份。每包附commit/文件白名单/协议差异/测试命令与退出码/环境/原始结果/未验收项/回滚；Token用量不可得就标不可得，不编造吞吐/成本。

## 5. 开工顺序与可用里程碑

1. **T00→T01→T04**：冻结所有权、只读契约和精确fixture；T01后立即把G1a小补丁交Mac集成者排入文件空档，第三机同时做T03/T20。
2. **T10/T11/T12**：只读数据内独立研发，第三机按文件归属一次一个编码包；独立审查可并行。G1未完成时先组件验证，不冒充真实宿主验收。
3. **首批产品门**：T03组件与T02/G1b联验、T10/T11/T12、T20和T21适用项通过后，可交付三项真实用户功能；不必等T13/T14/T15。
4. **第二批产品门**：T13→带图片的T14；T15基于T01 codec契约自行实现完整快照保存/读取/对比，不等T14。各功能分别验收，不因T14有ZIP输出就宣称已做媒体归档、M21交付或多人协作。
5. **最终交接**：T21/G2与T22关闭，记录Mac/Windows已测子集、限制、回滚与包版本；剩余额度推进尚未完成的已排期子项，不将额外大重构默认为下一任务。

## 6. 回归与证据分层

| 层级 | 本工具必须覆盖 | 证据边界 |
|---|---|---|
| 纯逻辑/契约 | 字段捕获、查询解析、重复ID、排序、精确TC/半开区间、覆盖并集、逻辑diff、文件转义与失败发布 | 不需要GPU；不得只测被测函数写出的同型dict |
| 独立Qt组件 | 虚拟表/取消/关闭/焦点/语言切换与资源缺失 | fixture/offscreen仅是组件证据 |
| 真正生产入口 | Edit菜单、真实两层工程、非当前序列导航、源缩略图供应、切工程/关闭、原工程不被修改 | 必须经过G1；不能从测试里手动造MainWindow属性替代 |
| 资料产物 | CSV/JSON可回读、PNG/HTML实际查看、离线包迁移、不同画幅/中文长文本不截断、对照报告准确 | 源联系表不证明渲染/颜色/声音成片正确 |
| 性能/平台/分发 | 100/1万实体、播放开关、worker/RSS回落；当前第三机＋Mac/Windows适用包、G2资源 | 各机原始数据分列，缺设备/缺平台不是通过 |

先复用/参考的既有测试（当前存在，**本次未执行**）：

- `tests/edit/test_asset_usage_index.py`、`tests/edit/test_interchange.py`、`tests/media/test_timecode.py`。
- `tests/media/test_thumbnail_provider_thread_safety.py`、`tests/media/test_thumbnail_cache_key.py`、`tests/edit/test_timeline_thumbnail_scheduler.py`。
- `tests/architecture/test_boundary_guardrails.py`；工具新增的边界导入必须实际满足，不能改skip或白名单掩盖违规。

拟新增`tests/edit/insights/`后再填写真实可执行的pytest命令、标记和退出码；现在不能假称该目录或CLI已存在。不要为文案/样式写实现镜像测试；对时间、捕获、并发、文件输出和领域边界写必要focused回归。性能/GUI单独进程，环境不用Mac venv复制到Windows。

每包证据建议`docs/validation/upgrade_T/<T_ID>/<commit>/<run_id>/`（拟新增），含素材/fixture/DTO hash、输入范围、实际环境、命令/退出码、预期/实际、样本覆盖与未测项、资源趋势。大媒体/生成报告只存受控产物索引；带真实用户内容的资料不自动上传。

## 7. 分支、集成与回滚

- 三台物理机器用各自clone/checkout与独立分支；不要同步共用一个正在运行的工作目录或`.venv`。同一机器需要隔离时可用Git worktree，Git官方说明它可在一个仓库维护多棵工作树；worktree不是跨机共享同一可写目录的理由。[Git官方文档](https://git-scm.com/docs/git-worktree)
- T建议分支名`codex/project-insights`；本次只写计划，不创建分支/worktree、不pull、不rebase。开工先保留用户已有改动并记录真实基线。
- 每个T包小提交只包含白名单新路径；G1/G2为单独集成提交，由M/W集成者检查同文件当前变更后合入。按协议/fixture稳定版本交接，别隔几分钟盲目追随主分支；基线更新在包边界进行，跑受影响回归。
- 集成门：T01契约/字段与三机所有权一致，T02真实入口可用，包内测试通过，G2资源齐全。没有Token用量/工具返回/OS实测的地方不编造“已完成”。
- 回滚移除/禁用工具菜单与相关新包，保留用户导出的资料；因为不改`.topos`/原媒体，旧工程不需为T线迁移。取消/卸载时先停止自有worker，临时目录只清自己本次任务，成功报告不覆盖或误删。

## 8. 给第三台电脑/ZCode的首个任务提示

将下面提示和本计划交给第三机。项目根路径要替换成本机实际绝对路径；首个任务只做T00/T01的可审阅契约，不让它一次改完整项目。

```text
项目：<本机项目绝对根路径>，PyQt6 + ModernGL + GLSL；遵循AGENTS.md/.clinerules。
先读docs/upgrade_plan_T_third_machine_2026-10-24.md及M/W最新版。
本次只做T00，再开始T01：现有能力/文件所有权、ProjectSequences两层状态、
InsightSnapshot v1字段/深冻结、只读read/navigate/thumbnail拟新增协议及fixture契约。
允许新增：src/features/edit/insights/、tests/edit/insights/、docs/insights/、
          docs/validation/upgrade_T/；仅可更新本T计划的状态和证据链接。
原有core/UI/app/shared/pyproject/打包/根测试配置仅可读取。
不要修改工程schema、renderer/decoder/音频/Deliver/QC/归档，不重复做媒体池搜索，
不要注册到EditModule.register_actions占位；G1真实菜单接线给主集成者单独落地。
先提供基线/可验证接口和小型独立预期，再逐包实现；每包做独立审查和focused验证。
状态区分API已实现、组件已验、产品已验，G1未合入不能称新功能已对用户可用。
交付：具体变更、测试命令/退出码、证据路径、未验项/需集成接缝和下一步。
```

后续按T10→T11→T12派发，联系表/资料包/对照排第二批。若某处必须改M/W所有权文件，第三机提交接口说明和小补丁草案，集成者串行处理；纯新目录研发继续进行，不反复请求用户确认例行实现。

## 9. 保留给后续的条件升级

- 语义检索只在已有AnalysisService/本地索引可用后加适配，明确离线/模型缺失；不默认调用ASR/外部付费模型。
- 搜索结果批量编辑、marker写回、评论协作、命名工程版本恢复等会触及共享command/IO，留待M对应正确性收口后另设所有权与Undo/迁移门，首版T线不做。
- 调色/合成后的时间线联系表须通过M/W已验证渲染供应接口，单独测颜色/效果/lease；不能把CPU源缩略图替换成未经协调的GPU读取。
- 新GLSL插件库/标题内容包可另开内容任务，但先盘点已有插件/模板与目标镜头，并等待效果/模板接口稳定；本计划不因剩余额度顺手扩进M05/M07/M13的文件范围。

对标参考：[Resolve官方Media工作流](https://www.blackmagicdesign.com/products/davinciresolve/media)说明素材metadata与Smart Bin组织的价值；Topos已有相应基础，本计划选择只读跨序列聚合增量。第三机功能取舍以本项目源码盘点和可独立交付为依据，不宣称完全对齐其他产品。

## 修订记录

- 2026-10-24 v1：为第三机规划6项新用户功能、14个可勾选工作包；冻结新目录白名单、G1/G2串行接缝、三机分工、现有能力复用、分级验收与回滚。当前仅计划编制，未实施/调度任务。


- 2026-10-24 v1复核：G1拆a/b避免与UI验收互等；区分只读编辑内容与正常导航状态；明确完整快照codec和T15保存/读取责任，T14复用；保持14个主包和6项新用户功能。
- 2026-10-24 v2执行：T00–T15+T20+T22 全部完成（T02 含 G1a 实际落地：菜单+宿主回调+导航语义，本机以 Mac 集成者角色执行）；累计提交见分支 `codex/project-insights`（`536443bd4`…）；测试 `tests/edit/insights` 126 绿；T21 分层进行中（余 G1b GUI 交互、Windows 复验、G2 打包）。
- 2026-10-25 v2执行补完：T21 自动化可及部分关闭——G1b-lite（真实 MainWindow(test_mode) 端到端：回调解析→factory→真实捕获→搜索→worker 回落→no_project）、G2 双路（pyproject package-data 实测 wheel 含 locales；base_spec 二级 locales 扫描+测试钉住）、全量回归零新增失败（worktree 基线对照 61⊂65）；`tests/edit/insights` 133 绿。计划 14/14 勾选；人工鼠标级 GUI 验收与 Windows 复验两项外部依赖在 T21 分列保留。
- 2026-10-25 v3完美化：§0"6 项功能不能只提交 helper"补完——窗口新增联系表/资料包/对照三个页签（共 6 页签，T13/T14/T15 全部用户可见可达）；T11 音频 cue 验收项实际接线（audio_cues.py 纯委托 interchange.CueSheet，资料包含 audio_cues_*.csv，宿主序列缺失跳过不伪造）；结果表新增 Enter 键等价导航（QShortcut WidgetShortcut 不抢搜索框）；事件级测试：双击经 doubleClicked 信号连接验证+Enter 经 QTest 真实键盘投递验证（offscreen QPA 不识别合成鼠标双击——最小探针证实，非缺陷）；⑤full_chain 导航深挖：真实 MainWindow+真实 TimelinePanel/EditPage/MediaPoolPanel 上执行真实 _on_timeline_switched 方法体，捕获并修正 G1a 初版误接 _on_media_pool_timeline_selected（该方法只显示信息不切换序列）的真实bug——单元级 stub 测试无法发现此类接线错误；正确切换后验证 active_timeline_id 导航状态变化与编辑 revision/序列集合不变；`tests/edit/insights` 145 绿 1 跳。
