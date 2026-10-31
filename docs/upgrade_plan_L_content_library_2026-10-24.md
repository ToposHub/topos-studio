# 升级计划 L · 剪辑、调色、合成内容库扩展

> 日期：2026-10-24；版本：v1；编制时观测 HEAD：35a9ff904，开工重新记录基线。
> 状态：2026-10-25 第三机开工——L00 已完成（基线/租约/逐项去重台账，见 `docs/validation/upgrade_L/L00_baseline.md` 与 `content_staging/library_v1/specs/L00_ledger.md`；Mac/W 接缝确认待其机器启动后补）。其余工作包未开始。
> 配套：[Mac 计划 M](upgrade_plan_M_mac_2026-10-24.md)、[Windows 计划 W](upgrade_plan_W_windows_2026-10-24.md)、[第三机计划 T](upgrade_plan_T_third_machine_2026-10-24.md)。
> 仓库根：/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01。下文源码路径均相对此根；在其他机器委派时先解析本机仓库根，再展开为绝对路径。

## 0. 目标、数量与三机排程

可以大幅扩充内容库。当前适合把富余 Token 投入算法差量设计、参数和视觉质量、独立参考实现、真实 GPU 输出、预设制作及审查。先一次规划完整内容，再按小包交付；每个包都能单独进入产品。

本计划提供 **59 张内容工作单＋80 个具名预设目标**，分为：

| 内容 | 工作单数 | 数量含义 |
|---|---:|---|
| 滤镜/调色算子 F01–F20 | 20 | 9 个新算法候选、10 项已有算法补强、1 项复用暴露；不是 20 个从零新增算法 |
| 视频转场 X01–X18 | 18 | 18 个算法家族候选；左右方向、材质、速度曲线不另算算法 |
| 合成节点 C01–C21 | 21 | 8 个专用算法候选、13 项共享算法或已有能力的节点入口；部分直接复用 F 项 |
| Look/滤镜配方 P01–P32 | 32 | 有工作空间、参数、强度、预览和可应用载荷的配方 |
| 转场配方 P33–P56 | 24 | 方向、图案、种子、参数组合；不是另外 24 个算法 |
| 合成宏 P57–P80 | 24 | 可插入节点图的组合模板；不是另外 24 个节点核 |

以上为**拟实施目标**。L00 要逐项按算法和参数去重；发现已有等价实现就复用并调整分类，保留差量记录，不为了达到数量复制文件。没有通过实际执行链的项目保持待集成/待验收。59 张工作单不能相加宣传为 59 个独立新算法，更不能据此宣称已补齐达芬奇所有效果。

| 机器 | 本轮职责 | 排程 |
|---|---|---|
| Mac / M | 继续既有正确性计划；作为共享主集成者，分小提交落地 L02–L05 的宿主接缝 | 不停下 M；给 L 的薄接缝安排串行窗口，不要求等整份 M 完成 |
| Windows / W | 继续原生链路计划；复验同版本内容的 GL、预览/导出、驱动与冻结包 | 核心问题仍按 W 优先级；内容按小批接收 |
| 第三机 / L | 内容规范、独立核/着色器、CPU 数学参考、夹具、预设、预览素材与文档 | 当前新增方向优先执行 L；原 T 洞察工具按工作包排在其后，保留 T 计划 |

第三机的 OS/GPU 未知：可以立即做规范、纯 CPU 参考和内容组件；真实 GPU 验收由具备相应设备的机器完成。无设备属于未验收。第三机同时保持一个写入任务，可另开独立只读审查/夹具设计；多个机器不直接同时写同一 checkout。

## 1. 当前盘点与必须先解决的接缝

本节为源码静态盘点；没有在本轮运行 GPU、Windows 或安装包。数量描述当前 checkout，不代表全数可用。

| 现有内容/机制 | 实际情况 | L 线处理 |
|---|---|---|
| Color 插件 | plugins/ 有 35 个 .tglsl、1 个 .tai；已有白平衡、grain、halation、glow、锐化、模糊、暗角、畸变、色散、CRT、glitch、半色调等 | 不把基础滤镜重写一遍；F 表逐项说明差量 |
| 发现与身份 | plugin_manager.py 用 os.listdir 非递归扫描，stem 保留大小写；同 stem 可能覆盖 | 暂存子目录不会自动上线；使用稳定前缀、冲突检测与明确发布路径 |
| Edit 滤镜 | EffectCatalog 目前注册 7 项内置单输入效果及 .tglsl 包装；.tai 不进入该 fragment runner | 复用 Catalog；不把 AI 插件当通用片段滤镜 |
| 跨页 GLSL ABI | dctl_wrapper 声明 u_texture/u_resolution/u_<参数>；现有插件使用裸参数和 u_input_tex/u_tex_size/u_aspect_ratio，静态未见完整别名/改写 | L02 用真实 Color＋Edit 编译/像素证明适配，先修共享接缝再批量声明跨页支持 |
| .tglsl 表达能力 | vec3 process(vec3, vec2) 是单输入 RGB 处理；不能直接写输出 alpha，也没有保证的历史帧或时间 uniform | 多源、matte、时域走对应原生接口；不得随手增加一个 u_time 就宣称宿主会供值 |
| 视频/音频转场 | 33 个视频 ID、4 个音频 ID；部分方向复用同核，当前 slide CPU mask 复用 wipe | 新增真正的 Push 等差量；4 个音频类型保持独立 |
| 转场 JSON | 用户 base 白名单仅 15 个已有类型；当前 checkout 无用户转场 JSON | 只可直接做旧算法配方；新增算法必须经 L03 注册、CPU/GPU、生产预览/导出链 |
| 旧转场差异 | smooth_cut 的 GPU 为模糊混合近似，CPU 进入光流；GLSL 路径还需核验 alpha | L00 记录能力差异，L03 处理本次接缝相关项，不假定旧 33 项已跨后端等价 |
| Composite | nodes/ 中 AST 盘点 98 个显式 node_type 类；静态 registry 99 键，含别名/延迟项；另有 2 个插件模块、3 个插件节点 | 注册数量不是已验收算法数量；基础 Blur/Keyer/MatteCleanup/LightWrap 等优先复用 |
| 新节点执行 | 自动发现 Python 插件不自动实现 GPU；未知 _effect_type 当前警告后返回输入 | L04 必须接真实 dispatcher、能力和导出消费者；无实现精确报错，不能以透传冒充完成 |
| 现有预设 | Color 为 .topos＋sidecar；Composite 有节点/连接 JSON；Edit 用户转场为特定 JSON | L05 定义转换/应用入口，不能发一堆任意 JSON 就称可用预设 |

现有 .tai 引用的 4x_ultrasharp.onnx 在本次 checkout 未找到，不能计为可运行超分。本计划不通过新增模型占位文件扩充数量。

### 1.1 与 M/W/T 的关系

- M05：完整调色链/Layer/Composite 消费；L 复用其顺序与载荷。L02 可以先验单效果，生产跨页链通过 M05 对应子项后才能标完成。
- M07：插件身份/版本/锁定；L01 登记身份协议，工程保存由已有机制及 M07 接入，不新增第二套锁文件。
- M13：关键帧和 Edit UI；L05 复用真实命令/动画入口，基础静态效果不等待所有高级动画完成。
- M19：工作/显示/Scope 色彩契约；F01/F05 等色彩敏感模式及最终跨页显示据其已验子集声明。
- M20/M21：归档依赖/输出事务；外部位移图、纸纹、字体等交其闭包，成片交其发布流程。无外部资源的算子不被整包阻塞。
- M23/UM02：跟踪、AI、时域增强；不在 L 重复增加同名占位功能。
- W07/W08/W10：分别接内容正确性、资源压力和跨机/离线恢复证据；本计划不改它们的任务状态。
- T：继续保留只读洞察工具的独占目录。L 和 T 是第三机先后领取的工作包，不要求同机同时运行两个写入者。

## 2. 文件所有权与发布方式

### 2.1 第三机独占区（拟新增）

| 路径 | 用途 |
|---|---|
| content_staging/library_v1/specs/ | 逐项规格、身份/能力 manifest、参数 schema、接缝提案 |
| content_staging/library_v1/filters/ | 单输入算法源/适配描述；每项独立子目录 |
| content_staging/library_v1/transitions/ | 双源 CPU 核、GLSL/MSL 草稿及参数映射；不复制整个时间线 |
| content_staging/library_v1/nodes/ | 节点类/端口描述、执行器小补丁草案；不是自动可执行 marker 集合 |
| content_staging/library_v1/presets/ | 80 项的规范载荷、宿主格式转换输入和资源引用 |
| content_staging/library_v1/references/、fixtures/、previews/ | 独立数学参考、小夹具、实际算法生成的预览；许可/hash 齐全 |
| tests/content_library_v1/ | 自有 focused/契约/视觉测试；不改父 conftest 和共享夹具 |
| docs/content_library_v1/ | 使用说明、算法区别、平台交接和发布记录 |
| docs/validation/upgrade_L/ | 以后每包的基线/结果/能力矩阵和证据索引；大视频只留可定位引用 |

开工确认目录未被他人占用。暂存区**不属于生产插件发现路径**，这是组件开发边界。测试 parser 时传显式暂存路径，不能用生产 reload 替换正在运行的全局 registry。

共享主集成者发布时：单输入 .tglsl 按当前非递归 loader 放到 plugins/LIB1_<slug>.tglsl；节点可发布为 plugins/node_plugins/topos_library_v1/ 包；转场独立实现可放入 src/features/edit/transition_packs/library_v1/，经现有转场管线的窄接缝调用。这些均为**拟定目标路径**，L00/L02–L04 根据当时依赖边界定稿，不是假定已经存在的接口。

如同一核同时供 Edit/Color/Composite 使用，由 Mac 集成者放入现有 shared effects 层的合适位置；上层不能反向导入 Composite 内部代码。第三机交源文件和小补丁草案，不自行搬动共享实现。

### 2.2 共享文件单一写入者

下列既有文件及同类调用链只由 Mac 主集成者串行修改；Windows 平台专属后端由 W 主责，涉及共同文件先交接：

- Color plugin_manager/shader_compiler、shared EffectCatalog/dctl_wrapper/effect_pass_runner/plugin identity；
- Edit transition_catalog/custom_transitions/timeline_v2、mask/full-frame/GPU spec/GLSL、预览和 Metal 导出；
- Composite node_plugin/node_engine/node_capabilities、base_evaluator/shader library、graph schema/migration/preflight、scheduler/resource pool/export；
- 宿主菜单/效果面板、共享 i18n、工程 schema、归档、打包/依赖、CI/全局 fixtures。

第三机不得顺手修改这些文件，或已有的 35 个 .tglsl 来规避 ABI。先提交接缝规范和最小可审阅变更，由对应主责合入。例行实现不需要反复请求用户批准。

## 3. 19 个工作包与依赖

“硬依赖”指本包进入生产的前提；源码/参考/夹具可在协议冻结后于暂存区开发。产品关闭还必须通过 L20–L22 的本次发布子集。P0 是基础或发布门，P1 是优先内容，P2 是后续/较复杂内容。全部初始未开始。

| 状态 | 包 | 优先级 | 硬依赖/关闭条件 | 主责与交付 |
|---|---|---|---|---|
| ☑ | L00 基线、逐项去重与文件租约 | P0 | — | 第三机盘点已完成（59 项台账+分类修正 C10/C15/C16）；Mac/W 接缝确认待补 |
| ☑ | L01 内容 manifest、参数契约与独立夹具 | P0 | L00 | 第三机；身份、schema、能力及黄金输入/手算预期——已完成（specs/spec_schema.md+manifest.py+fixtures 9 项带 hash 清单+24 测试；time_domain 全库冻结 static） |
| ☑ | L02 单输入滤镜跨页适配与试点 | P0 | L01；M05/M07/M19 按对应子能力 | Mac 集成，第三机供 F16/F19；点处理与纹理邻域两类真实执行——已完成（dctl_wrapper 别名层+全库 35 插件真实编译+F16/F19 像素对拍+LIB1 双插件发布注册；报告 docs/validation/upgrade_L/L02_seam_report.md；M05.2 载荷矩阵口径保留） |
| ☑ | L03 双源转场接缝与试点 | P0 | L01 | Mac 集成；X01 mask＋X09 双源重采样，CPU/GL及可支持导出——已完成（transition_packs 生产核+catalog/mask/GPU spec/GLSL/预览导出接线，真实 GL 对拍，MSL 显式回退；报告 docs/validation/upgrade_L/L03_seam_report.md；端到端导出/保存重开归 L20） |
| ☑ | L04 节点执行/能力接缝与试点 | P0 | L01；C01 共享核依赖 L02 的 F16 子门 | Mac 集成；C01/C17——已完成（marker→shader→library→evaluator→dispatch→registry(101键)→capability 全链+未知 lib1_* 精确拒绝红线+真实 GL 乒乓链对拍 1.5e-8；报告 docs/validation/upgrade_L/L04_seam_report.md；完整 evaluator 消费链/分块归 L20） |
| ☑ | L05 内容库入口、预设转换与应用 | P1 | L01；各内容入口只启用已过 L02/L03/L04 的能力 | Mac 改宿主，第三机供描述/资源；每类先 1 个真预设——已完成（白名单扩展+user_* push 分流修复+内置合成预设+P29 双载荷转换；报告 docs/validation/upgrade_L/L05_report.md；.topos 端到端落盘/宏事务归 L18/L20） |
| ☑ | L10 滤镜 A：逐点与低成本差量 | P1 | L02 对应点处理子门 | F01/F02/F03/F04/F12/F16/F17/F18，共 8 项——已完成（暂存四件套+oracle↔独立手算双轨+manifest；真实 GL 对拍随 L16 集成批1 落地（C02/C04 即 F 通道共享核）；发布态归 L20/L22） |
| ☑ | L11 滤镜 B：空间/邻域与质感 | P1 | L02 纹理子门；多 pass 按额外子门 | F06/F10/F13/F14/F15/F19/F20，共 7 项——已完成（暂存交付+F06/F14 单 pass 降级口径声明；C07 hatching GL 对拍绿随批3b；多 pass 原生 renderer 归后续） |
| ☑ | L12 滤镜 C：色域、时序身份与镜头模拟 | P2 | L02；F05 依赖 M19 色域子契约；动态模式依赖正式 frame/seed 子门 | F05/F07/F08/F09/F11，共 5 项——已完成（静态版全交付；seed 静态确定性钉住，动态过子门前不声明） |
| ☑ | L13 转场 A：常用揭示/Push | P1 | L03 对应 mask/重采样子门 | X01/X02/X03/X05/X06/X08/X09/X13，共 8 家族——已完成（暂存+生产集成：mask 五家族 GPU kind6-10 真实 GL 对拍含 uint 散列逐位；X05 首个内容驱动 mask；交接语义修正 X02 退化 wipe/X03 非棋盘均独立测试抓出并三处同步重算） |
| ☑ | L14 转场 B：几何、折射与图案 | P2 | L03；X14 外部 map 模式另需辅助采样执行子门与资源闭包 | X04/X07/X10/X11/X14/X15/X16/X17，共 8 家族——已完成（CPU 复合全家族生产集成统一分派+GPU kind12-21 全落地真实 GL 对拍（X14 Cell 档容差如实登记）；X14 外部 map 模式按计划不在本批（资源子门声明）；X07 GPU kind11） |
| ☑ | L15 转场 C：卷页与灼烧 | P2 | L03；必要多 pass/资源子门 | X12/X18，共 2 家族——已完成（X12 卷页 GPU 三区合成对拍；X18 rank+发热复合 Field/Edge 双档对拍闭合——初版 Edge 失配经对读定论为 fixture 刀锋：w=24 时 gp_x 列 i≡1(mod3) 恰落精确半整数致 floor 平局按列翻转，fixture 改 w=22 后双档精确，公式本就一致） |
| ☑ | L16 节点 A：复用共享算法 | P1 | L04；相应 F/已有核真实可执行 | C01–C10、C15/C16/C20，共 13 入口——已完成（C02-C10/C15/C16/C20 九节点过 L04 全链真实 GL 对拍 oracle（C06 三轮收口：oracle 整数格+bilinear clip-pos 语义）；mix→amount 保留字先例；oracle 缺陷 2 处修复留痕） |
| ☑ | L17 节点 B：合成专用能力 | P1/P2 | L04；多输入/多输出/全帧算法各自子门 | C11/C12/C13/C14/C17/C18/C19/C21，共 8 项——已完成（三子门全部落地：多输入 marker 契约（_effect_input_map/_effect_mask 进 dispatch）、多输出双占位→normalize 别名、全帧 CPU 通道（回读→计算→上传，fusible=False）；第二轮对读全部收口：真因=分派器采样器 uniform 名错写（u_map_unit→u_map_tex 等）、C11/C14 shader 行序口径（GL-natural 下 row=(1−v)·H）、C21 α 模糊+裸纹理误用、测试双重 flipud/fixture 刀锋——生产缺陷 9 处修复+dispatcher 级新测试 3 项，xfail 清零，详见发布门报告收口表；texture_id 绑定契约修复） |
| ☑ | L18 80 项配方与宏 | P1/P2 | L05；每项只依赖其实际使用算法/资源 | P01–P80——已完成（80 项全交付：39+40 新配方+P29 试点；转场 24+宏 24+插件 16+暂存组件 14+节点复用 1；依赖全真实存在逐一钉住；白名单外不产用户转场 JSON；status 全 component_ready 如实；push_frame 垂直非方幅生产缺陷由配方批次发现并修复） |
| ☑ | L20 真实工程/跨页/导出验收 | P0（发布门） | L02–L05 本次声明子集＋对应内容 | Mac 主验，W 复验；每个发布 ID 都有真实消费者证据——**Mac 主验完成**（test_l20_acceptance.py：能力矩阵 23 条真实消费者证据=转场 16 家族生产分派真实帧+mask 6 家族真实消费+节点四类子门真实 dispatch+配方 3 条；18 ID catalog/回退集一致钉住；报告 docs/validation/upgrade_L/L20_L21_L22_gates_report.md；W 侧复验待执行） |
| ☑ | L21 性能分档、资源/取消与播放压力 | P0（发布门） | 候选内容可执行；不等待未发布算法 | Mac/W；成本/基准、缓存/压力、取消/关闭——**Mac 侧完成**（test_l21_performance.py：1080p 分档基准 9 项如实声明 CPU 导出档口径（实时预览走 GPU 已对拍）、全帧通道资源纪律、100 帧播放扫描；W 侧待执行） |
| ☑ | L22 打包、缺包/升级/归档往返 | P0（发布门） | L05＋本次支持集；M07/M20/M21 相应子能力 | Mac/W；干净包、离线工程、旧版本兼容——**Mac 侧完成**（test_l22_packaging.py：暂存隔离红线子进程验证、wheel 含新增生产模块且不含暂存区、配方 JSON 归档往返消费者可执行；W 侧待执行） |
| ☑ | L23 使用说明、示例与最终台账 | P1 | 对外声明项通过 L20/L21/L22 | 第三机整理，集成者复核；真实内容数量/限制/证据——**集成者复核完成**（使用说明 docs/validation/upgrade_L/library_v1_usage.md=转场 18 家族/节点 21 入口/配方 80 项只声明已验能力+已知限制；发布门报告 L20_L21_L22_gates_report.md 含收口队列与未竟事项如实登记） |

先做 **F16 GradientMap＋F19 Median、X01 时钟＋X09 真 Push、C01 GradientMap＋C17 EdgeExtend** 六个试点：覆盖逐点、邻域、mask、双源 UV、共享核和 alpha。试点计入下面清单，不额外加数量。L02 的点处理子门通过后 C01 即可接入，不必等 Median 或全库结束。多输入用 C12、多输出用 C21 再开各自子门。

L01 夹具先于试点实现；L05 只需每类一个试点载荷即可落地，不等待 80 项；L18 逐项等其算法，不与 L05 相互等待。组件等待接缝时继续下一个规范/参考/预设，不能把“暂存文件写完”改成“产品完成”。

## 4. 20 项滤镜/调色算子工作单

分类：N＝新算法候选；E＝现有算法明确差量；W＝复用现有核提供入口。所有项初始未开始。范围以 L00 的参数级去重为准。

入口原则：适合单输入 RGB 的项目以 Color＋Edit 为目标；Composite 通过对应 C 入口或现有共享效果入口使用。需要多 pass 的项目采用原生 EffectSpec/renderer 支持，**不强塞成一份 .tglsl**。CPU 数学参考只用于验证，默认发布 OpenGL 能力；CPU/Metal/D3D/CUDA 只有实际生产实现验过才声明。

| ID / 建议稳定 slug | 分类/包 | 功能差量、关键参数 | 额外验收点 |
|---|---|---|---|
| F01 wb_adaptation | E / L10 | 在 White_Balance_Advanced 之外补可解释的 source/target white、temperature/tint 与选定色适应模型 | 白点/工作空间冻结；中性灰与参考变换对拍，不把通道 gain 称物理色适应 |
| F02 subtractive_density | N / L10 | 胶片风格减色密度与通道交叉污染；density、dye coupling、luma protection、mix | 与 saturation/Cross_Process 做视觉和数值差量；不宣称某商业胶片的精确光谱仿真 |
| F03 protected_vibrance | E / L10 | 独立肤色保护权重、肤色中心/宽度、亮度保护与自然饱和强度 | 保护权重可视化走允许入口；肤色/彩色灯光/非肤色排除，滑杆实际有效 |
| F04 zoned_tint | E / L10 | Three_Way_Tint 的可移动分界/交叉宽度与分区强度 | 分区连续，中性设置恒等；不能仅复制固定三段 |
| F05 gamut_rolloff | N / L12 | 指定 RGB gamut 内的色相保持高饱和压缩；threshold、knee、strength | 与 CST/ODT 分工明确；负值/超白策略及边界参考，禁止悄悄裁到 0–1 |
| F06 multiscale_halation | E / L11 | 现有 halation 的多尺度扩散、阈值、红色散射权重与能量控制 | HDR 点光源/尺度变化/边缘；多 pass 资源和 ROI/padding |
| F07 seeded_grain | E / L12 | 亮度相关/彩色相关的可复现颗粒；size、strength、chroma、seed、animation mode | 静态版先验；动态 frame/seed 来自正式上下文，随机 seek 与冷热缓存一致 |
| F08 gate_weave | N / L12 | 门片横纵位移/微旋转模拟；amplitude、frequency、seed、border mode | 不读取系统时钟；确定性帧身份、像素/画幅单位、裁边/透明策略 |
| F09 dust_scratches | N / L12 | 灰尘/划痕/边缘缺口；density、length、age、seed、opacity | 程序静态版先行；动画另验 frame 契约；外部纹理版交依赖闭包 |
| F10 starburst | E / L11 | 从 Anamorphic_Streak 扩展多叶/多方向高光衍射；blades、angle、length、threshold | 方向数为有界常量档；与水平 streak 区分，极亮输入能量有限 |
| F11 lens_breathing | E / L12 | 可动画的呼吸式缩放/中心/轻微畸变；amount、pivot、border | 先提供静态参数＋既有关键帧；不声称自动估计真实镜头焦距 |
| F12 palette_dither | N / L10 | 调色板量化＋有序/固定蓝噪抖动；palette、levels、pattern、scale | 区别 Pixel_Blocks；固定资源/hash、色域/显示域与 HDR 映射策略明确 |
| F13 print_registration | E / L11 | Halftone_Print 的通道套印偏移/网点角度/纸底；先核已有参数 | 高频走样、角度/像素比例；CMYK 风格模拟不替代印刷 ICC 流程 |
| F14 hatching | E / L11 | Edge_Ink_Outline 之外增加亮度驱动交叉排线/纸纹；spacing、angle、layers、ink | 平坦灰阶也产生可解释排线，不能只重命名描边 |
| F15 watercolor | N / L11 | 局部扩散、颜料聚边与纸纹；diffusion、edge pigment、paper scale、mix | 有独立扩散模型/质量样片；有界核/多 pass，不能用 blur＋saturation 冒充完成 |
| F16 gradient_map | E / L10 | 将 Duotone 扩为多色停靠点的亮度映射；stops、positions、interpolation、mix | 最小试点 4 个色标；schema 不能表达时走原生参数适配，不把数组塞进现有 slider |
| F17 solarize | N / L10 | 可控阈值折返/反相、软过渡、通道选择与 mix | 明确处理域、阈值外 HDR 行为；强度零恒等，非 Invert 换名 |
| F18 posterize_shared | W / L10 | 复用现有 Posterize 节点算法，提供一致的 Edit/Color 入口 | 共享核不反向依赖 Composite；levels/量化策略一致，不再造第二个 Posterize 节点 |
| F19 median_rank | N / L11 | 小核中值/百分位滤波；3×3/5×5 档、percentile、luma/RGB 模式 | 独立手算排序预期；不是 bilateral，常量有界采样和 ROI padding |
| F20 convolution | N / L11 | 受限 3×3/5×5 自定义卷积；kernel、normalization、bias、border、mix | kernel 编辑为原生 schema/UI；identity/负权重/零和核、alpha 策略与精度 |

F07/F08/F09 的动态模式不依赖相邻帧，但依赖确定性 frame/seed 接口；没有该接口时只发布静态模式。真正曝光拖尾、光流模糊、时域降噪需要历史帧 provider，不列为这些单帧效果已完成的能力。

E 项也要版本化，保留旧工程旧算法语义。优先新增明确版本的实现/入口，而非直接让旧 ID 的相同参数突然改变成片。是否升级原插件由 M07 的兼容策略决定。

## 5. 18 个视频转场家族

以下均为 N 候选；分类 mask＝两源混合权重，sample＝两源重新采样，composite＝额外颜色/遮挡合成。最终分类以真实生产路径定稿。不能以单输入 EffectSpec 代替转场实例。

| ID / slug | 包/路径类型 | 最小功能/参数 | 与已有算法的差量与验收 |
|---|---|---|---|
| X01 clock_wipe | L13 / mask | 极角推进；center、start angle、clockwise、softness | 不是缩放 iris；角度接缝/中心连续；首个 mask 试点 |
| X02 venetian_blinds | L13 / mask | 周期百叶；count、angle、phase、softness | 多条带揭示；方向仅配方，条数极值不除零 |
| X03 checker_reveal | L13 / mask | 二维交错棋盘；rows、columns、phase、softness | 二维交错推进，不是单幅 wipe |
| X04 staggered_tiles | L14 / sample | 分块错峰/缩放；grid、delay、order、seed | 每块独立时序；采样 UV 真变化，与 X03 mask 不同 |
| X05 luma_reveal | L13 / mask | 按 source A/B 亮度排序；source、threshold span、softness、invert | 以图像内容驱动；常量亮度和 HDR 输入仍有确定端点 |
| X06 noise_dissolve | L13 / mask | 程序 rank-field 揭示；noise mode、scale、seed、softness | 固定种子，噪声连续性可控；区别已有 film dissolve |
| X07 voronoi_reveal | L14 / mask | 晶胞距离推进；cell count 档、seed、edge width | 有最近种子/距离场语义，不是噪声图改名 |
| X08 halftone_reveal | L13 / mask | 网点扩张揭示；spacing、angle、shape、softness | 网点频率独立，分辨率/像素比例一致 |
| X09 true_push | L13 / sample | A/B 同时平移推出/推入；direction、easing、border | 区别当前 slide→wipe；两源特征点轨迹都移动，首个 sample 试点 |
| X10 hinged_doors | L14 / sample+composite | 双门/合页透视；hinge、perspective、shadow、backface | 投影、遮挡/背面有明确模型，先单帧 2D 投影实现 |
| X11 cube_faces | L14 / sample+composite | 两面透视旋转；axis、perspective、shadow | 真实两源映射到相邻投影面；不新增通用 3D 引擎 |
| X12 page_curl | L15 / sample+composite | 非线性卷页；direction、radius、back color、shadow | 非线性曲面 UV/卷边；不以线性翻转替代 |
| X13 pixelate_bridge | L13 / sample | 分块采样变粗→换源→恢复；cell size、curve、source switch | 动态量化时序，区别普通 pixel filter；进出端点恢复精度 |
| X14 displacement_bridge | L14 / sample | RG/XY map 驱动两源位移；scale、map space、seed | 程序 map 可先发；外部 map 必须绑定资源/hash、预检和归档 |
| X15 ripple_refraction | L14 / sample | 径向波场折射；center、frequency、amplitude、damping | UV 波场不是 radial blur；进出位移归零 |
| X16 scanline_slices | L14 / sample | 分行带错位/换源；band size、amount、seed、curve | 分段 UV 与源选择，非 RGB split 换名 |
| X17 kaleido_bridge | L14 / sample | 角域折叠/镜像到完整 B；sectors、rotation、center、curve | 中段空间折叠、两端原画；不能始终残留万花筒 |
| X18 burn_reveal | L15 / mask+composite | rank-field 外加焦边/亮边；pattern、char width、emission、seed | 与 X06 区别由实际焦边/发光证明；HDR 能量/颜色空间明确 |

每家族只计一个算法，左右上下/CW/CCW/材质预设不再计数。新视频转场默认不隐式加音频 crossfade；用户选择联动音频时，调用既有音频命令和曲线，另行验采样边界。

## 6. 21 项合成节点

W 表示入口增量，复用核仍须真正接执行器、参数、端口、图保存和导出；不是“只写一个 Node 类”。N 为初筛专用能力，L00 仍需复核。

| ID / 建议 node_type 后缀 | 分类/包 | 实现范围、关键参数/端口 | 共享/特殊验收 |
|---|---|---|---|
| C01 gradient_map | W / L16 | image＋可选 mask；多色亮度映射 | 复用 F16；不是 ColorLookup 的 RGBA 曲线；首个共享节点试点 |
| C02 rgb_matrix | W / L16 | 显式 3×3/3×4 通道矩阵、bias、alpha policy | 复用 Channel_Mixer/已有通道数学，identity/通道交换对拍 |
| C03 despill | W / L16 | 独立去溢色、选择颜色、亮度恢复、mask | 从既有 keyer 提取/复用；不是新 keyer 算法 |
| C04 solarize | W / L16 | 阈值折返/软过渡/通道/mix | 复用 F17，参数与工作域一致 |
| C05 mosaic | W / L16 | block size、grid origin、mask、border | 复用 Pixel_Blocks；非整块边缘与 alpha |
| C06 halftone | W / L16 | 网点/网线、角度/套印 | 复用现有＋F13，避免重复 shader |
| C07 hatching | W / L16 | 亮度/边缘排线与纸纹 | 复用 F14，像素比例/分辨率一致 |
| C08 crt | W / L16 | 扫描线、栅格/辉光组合 | 复用 CRT_Scanlines，显示域声明 |
| C09 glitch | W / L16 | 行/块位移，seed、frame mode | 复用 Glitch_Blocks；动态仅在正式 frame 子门后开启 |
| C10 kaleidoscope | W / L16 | 扇区数、镜像、中心/角度 | 复用 Kaleido_Mirror，边界/奇点 |
| C11 polar_coordinates | N / L17 | Cartesian↔polar，中心/角度/半径/滤波 | 接缝/中心奇点/反变换；bbox/采样域 |
| C12 vector_displace | N / L17 | image＋RG/XY vector image＋可选 mask；scale、neutral、units | 与 MeshWarp 区分；首个多输入子门，输入角色不能串线 |
| C13 stmap | N / L17 | image＋UV map；channel mapping、origin、invalid UV、filter | 与相对位移区分绝对坐标；map 空间/精度/alpha 与外部资源 |
| C14 tile_array | N / L17 | repeat/array count、spacing、offset、layout bounds | 不是 Crop 边界 repeat 换名；明确输出 bbox 和空隙 alpha |
| C15 median_rank | W / L16 | 小核中值/百分位 | 复用 F19；padding、整帧/分块一致 |
| C16 convolution | W / L16 | 3×3/5×5 kernel、边界/归一化 | 复用 F20；有效 kernel 编辑和负权重 |
| C17 edge_extend | N / L17 | 根据 alpha 把前景 RGB 延伸至透明区；radius、threshold、output mode | 首个 alpha 子门；默认保留原 alpha，去黑/白边样片，有限扩张半径 |
| C18 distance_matte | N / L17 | alpha→有符号距离场/边缘带/重建；metric、radius、threshold | 不等同 erode/dilate；距离单位、float 输出表示、分块 halo；无法证明 tile 支持就声明 full-frame |
| C19 fill_holes | N / L17 | matte 孔洞填充；connectivity、max area、threshold、output mode | 连通区域/面积语义；触边背景不误填，全帧算法预算/取消 |
| C20 halation | W / L16 | 独立高光散射节点 | 复用 F06；与普通 Glow 差量一致 |
| C21 frequency_separation | N / L17 | low/high 两输出；radius、filter、reconstruct mode | 首个多输出子门；low＋high 在声明域重建输入，high 允许负值；不是 Sharpen 预设 |

node_type 使用稳定 namespace，例如 lib1_gradient_map，最终由 L01 冻结；不覆盖现有类型。C12/C13 是多输入，C21 是多输出，C18/C19 可能全帧，不能套用单输入通用 marker 后直接宣称完成。

## 7. 80 个具名配方与组合宏

每个 ID 必须有独立的参数载荷、适用素材/工作域、强度建议、缩略图/对比样片和依赖 manifest。下列为可执行制作题目，具体数值由参考样片调试确定。不是只交名称清单，也不承诺模拟某商业品牌插件/胶片。

### 7.1 P01–P32：32 个滤镜/Look 配方

| ID 范围 | 四个具名目标（按编号顺序） | 主要复用 |
|---|---|---|
| P01–P04 | 自然人像、暖肤保护、冷环境自然肤色、柔和低反差人像 | 既有 WB/Vibrance/Three_Way；F03/F04 可用后增强并版本化 |
| P05–P08 | 柔和胶片、硬朗漂白、低密度暖片、冷影暖高光 | 既有 Bleach/Cross_Process/曲线；F02 |
| P09–P12 | 蓝调夜景、暖灯夜景、日转暮色、压暗晴日 | 既有 Day_Night/Grad_Filter/曲线 |
| P13–P16 | 青橙克制、粉绿分色、暖影冷光、单色银调 | 既有 Three_Way/Duotone/曲线 |
| P17–P20 | 微光柔化、逆光红晕、夜景星芒、霓虹扩散 | 既有 Glow/Halation＋F06/F10；不另造光晕算法 |
| P21–P24 | 细颗粒纪实、粗颗粒怀旧、旧片灰尘、轻门片抖动 | F07/F08/F09；静态/动态明确区分 |
| P25–P28 | 限色抖动、彩色套印、铜版排线、纸纹水彩 | F12/F13/F14/F15 |
| P29–P32 | 四色渐变、海报量化、局部太阳化、柔双色调 | F16/F18/F17＋既有 Duotone |

### 7.2 P33–P56：24 个转场配方

| ID 范围 | 四个具名目标（按编号顺序） | 主要复用 |
|---|---|---|
| P33–P36 | 顺时钟轻擦、细百叶、棋盘错落、网点展开 | X01/X02/X03/X08 |
| P37–P40 | 左推、右推、上推、下推 | X09，同一算法的四个方向 |
| P41–P44 | 细噪溶解、粗噪溶解、雾状揭示、块噪揭示 | X06；不同 rank-field 参数/程序图案 |
| P45–P48 | 亮部先入、暗部先入、柔阈值叠化、高反差亮度擦除 | X05；源选择/阈值/softness |
| P49–P52 | 水波折射、玻璃位移、扫描切片、万花筒桥接 | X15/X14/X16/X17；外部 map 可由程序生成固定资源 |
| P53–P56 | 轻翻页、焦边灼烧、晶胞揭示、柔影合页 | X12/X18/X07/X10 |

P41–P44 若只是同一噪声参数微调，仍明确标配方，不再追加“新噪声算法”数量。转场缩略图/GIF 必须从生产等价实现与固定输入生成，不能用无关动图替代。

### 7.3 P57–P80：24 个合成宏

| ID 范围 | 四个具名目标（按编号顺序） | 主要复用 |
|---|---|---|
| P57–P60 | 去白边前景、去黑边前景、细边缘保色、半透明边缘整理 | C03/C17＋既有 MatteCleanup/Keyer；质量样片不等于保证自动发丝分割 |
| P61–P64 | 低频肤色整理、高频细节保留、柔纹理重组、局部质感增强 | C21＋既有 Grade/Mask/Merge；提供 low/high 重组与手动 mask |
| P65–P68 | 双列画中画、九宫格海报、极坐标隧道、重复阵列背景 | C14/C11＋既有 Transform/Mask；不同素材插槽与输出 bbox |
| P69–P72 | 发光轮廓、单色剪影光、逆光包裹、柔辉标题 | 既有 Edge/Glow/LightWrap/Text/Merge；复用已有文字与灯光能力 |
| P73–P76 | 玻璃折射合成、热扰动合成、UV 贴图替换、纹理扭曲叠加 | C12/C13＋既有 Merge；固定资源/hash、替换插槽 |
| P77–P80 | 复古 CRT、彩色套印海报、排线插画、碎块故障 | C06/C07/C08/C09＋既有基础节点 |

宏导入必须重映射 node ID/连接/表达式引用，外部素材使用显式插槽；不能复制作者机器的绝对路径、源媒体或工程秘密。一次应用形成一次可 Undo 的领域事务。

## 8. 基础工作包的落地要求

### L00 · 基线与差量

- 重新读取 AGENTS.md/.clinerules、M/W 当前进度和 git 状态；记录 base/head、Python/Qt/GL/驱动/后端，保护已有未提交工作。
- 为 F/X/C/P 每项建台账：ID、分类、已有等价实现/参数、目标页面、主责、真实依赖、状态、证据。不以文件名搜索结束去重，要查看算法/参数/调用方。
- 按 3–5 项一个小提交分组；先冻结单一写入者/新目录租约与接缝队列。发现 M/W 范围缺陷交接，不在内容分支修复。

### L01 · 统一规格与独立参考

每个算法/入口至少有以下字段；这是拟定交付契约，由现有 parser/registry 适配，不是新造万能插件引擎：

| 字段组 | 要求 |
|---|---|
| 身份 | pack_id、stable effect/node/transition ID、version、source/content hash、参数 schema hash、最低宿主契约、未知版本保留策略 |
| 参数 | 类型/default/min/max/options、单位、neutral/bypass 语义、可动画性、输入验证；NaN/Inf/非法枚举明确处理 |
| 图像 | 输入/输出工作域/gamut/transfer、精度、alpha straight/premult、输出范围、采样边界、尺寸/像素比例、bbox/ROI/padding |
| 端口/时间 | 输入角色/可选 mask/缺输入行为、输出名/类型；time_domain/origin、有理 fps/帧时间、持久化 seed/资源依赖；无时域协议即 static |
| 能力 | 每页面、每后端、预览/视频导出/LUT 烘出/瓦片/动画分别列 supported/unsupported/untested；CPU oracle 与生产 CPU 后端分列 |
| 资源 | 纹理/字体/map/模型来源、许可、hash、相对资源 ID、缺失诊断及归档规则 |
| 验证 | 独立 oracle/手算向量、实际 GPU 输出、基准、用户流程、已知限制、每平台环境与命令 |

默认 identity 仅要求那些数学上有 neutral 设置的效果；源生成/matte 输出等没有自然恒等默认值时，明确 bypass 行为，不能强行写无意义 identity 测试。所有可见滑杆必须在规定素材/工作点有真实像素影响。

夹具至少有线性灰梯度、色块/肤色、HDR 点光源、透明彩边/发丝样片、UV 网格/定位文字、确定性短序列及授权纹理。输入与预期分别记录 hash；参考不能调用被测 GPU 函数生成自己的预期。

动态项逐项冻结 time_domain（source presentation、clip-local 或 timeline）和持久化 time_origin、fps 分子/分母、帧/子帧采样规则、instance_seed 与复制/分割的继承政策。不得用墙钟、播放次数、seek generation、Python 进程随机 hash 或临时对象 ID 生成噪声。静态图固定效果时间为 0，不从窗口重绘推帧；默认 seed 为参数中的固定值，复制沿用。如提供每实例随机模式，只在创建时生成并持久化 seed。修剪/分割/变速/嵌套后按已选域验证连续或有意重置的结果；同一持久化身份和效果时间在随机 seek、并行导出及跨页必须相同。revision/generation 用于失效与拒收过时结果，不能无故改变颗粒图案。

### L02 · 滤镜接缝

- 分析 Color 编译器→PluginManager→EffectCatalog→Edit runner 的参数命名、纹理 alias、分辨率/宽高比与上游输入；用 F16/F19 真编译/真渲染，不能只 regex 查头或伪造 uniform。
- 同一效果叠两次、两个不同插件同名局部变量、多页面/多渲染上下文验命名隔离；共享 shader 源可以，Program/FBO/VAO 不跨 context 复用。
- 原生数组/矩阵/多 pass 参数由宿主 schema 适配；不支持时明确限制/暂缓该模式。动态 frame/seed 是单独子门，未接入不发布动态效果。
- 外部纸纹/蓝噪等另开辅助采样资源子门：资源解析/有界解码缓存→宿主渲染线程上传→sampler 绑定→尺寸/通道/数据域或颜色域/滤波→失败与取消/上下文重建释放。资源 hash 进入身份和缓存；既有 EffectSpec 不能表达时由主集成者做窄适配，不能把文件路径塞进 params 就算支持。未过门只发布程序纹理或已验证内置资源模式。
- 生产能力从实际实现推导；metadata 写 all/video 不自动产生 CPU/Metal/D3D 支持。L20 验证最终导出，不以预览编译替代。

### L03 · 转场接缝

- 由现有 catalog/TransitionInstance/渲染计划进入 CPU mask/full-frame、GPU spec/GLSL、实际预览/导出；Metal 是独立实现/能力，缺实现明确拒绝或走已验证显式回退。
- 先落一个窄 descriptor/dispatch 适配，容纳本库定义；不建平行时间线或把 EffectSpec 改成无边界万能容器。
- 冻结 A/B、progress、时间对齐、frame/seed、尺寸/alpha 与参数 snapshot；单边/非邻接/缺 handles 的策略沿用已验证命令，不在 shader 猜源帧。
- X14 外部 map 必须通过 L02 同等的辅助 sampler 运行时子门，并在双源 A/B 之外绑定正确资源角色；数据 map 不误套显示变换。M20 归档闭包只证明资源可恢复，不证明它已被 GPU 采样。未通运行时门时仅提供程序 map 模式。
- X01/X09 必须过切点逐帧、源 UV 轨迹、保存重开/Undo 和实际导出；随后同类内容可小批复用接缝。

### L04 · 节点接缝

- 从插件发现/Node/ParamDef/Port 到 graph snapshot、capability/preflight、dispatcher/shader、真实 CompositeExportRenderer；新增 marker 必须有真实执行分支。
- 未支持效果精确诊断并保留节点/参数；不得警告后透传并报告成功。不要让 registry-only 默认能力冒充后端支持。
- C01/C17 验单输入/可选 mask/alpha；C12 另验多输入，C21 另验命名双输出和消费者。C18/C19 全帧/全局算法单独预算，不硬称 tile-safe。
- C12/C13 的 map 图像优先由既有 ImageInput/图端口供应，按数据纹理解释；涉及包内辅助纹理时复用已验证资源子门，不能在节点内另造无生命周期的文件加载器。
- 纹理/FBO 使用宿主池与渲染线程；节点对象不另建 GL 上下文，不把 GPU 句柄存入工程。

### L05 · 用户可用的内容库与预设

- 复用既有效果/转场/节点面板：分类/搜索/标签、收藏/最近使用、中文英文名称、说明/参数单位、效果成本和能力提示；避免另建三套相同浏览器。
- 提供实际应用、强度/旁路/重置、参数编辑/键帧、复制粘贴/替换；调用既有 command/service。涉及 M13 的动画模式按已验能力启用。预览悬停/参数试调有取消和播放避让，不提前写工程。
- 先让三类各 1 个配方真正转换并加载：Color .topos/sidecar、Edit 既有效果/转场载荷、Composite nodes/connections JSON；源配方 manifest 不冒充宿主格式。
- 内置只读库与用户自建预设分开定位；共享集成者决定资源路径/装包/版本迁移，不依赖当前工作目录，也不改用户已有预设。
- 应用时把实际参数及算法/包版本/资源身份固化到工程的现有模型；更新内置默认值、删外部 JSON 后重开不能改变原成片。
- 宏插入和批量应用预检所有目标、锁轨和依赖，成功才一次提交；失败零部分写入。收藏/最近使用走本机设置，工程内容走领域事务。

## 9. 每项内容共同验收门

### 9.1 画质、HDR 与 alpha

- 数值覆盖 -0.25、0、0.18、1、4.5、12 及高饱和色；有限数，无非设计裁剪。显示域艺术效果若需要映射到有限域，要显式给域转换/恢复策略，不暗中损失 HDR。
- mask/混合权重可以按定义限幅，图像 RGB 不能因此统一限到 0–1。比较在同一工作域的浮点像素进行；显示缩略图不能证明 HDR 数值正确。
- alpha 为 0/部分/1，分别验 straight/premult 和黑白背景合成；unpremult 零 alpha 处理明确。原 alpha 透传不自动证明 RGB 边缘正确。
- 横屏/竖屏/非方像素/奇数尺寸/1px/极小图、边界/超大 radius、部分 mask、不同输入尺寸；每个空间效果冻结像素或画幅单位。
- .tglsl RGB ABI 不承担 alpha 生成；需要 matte/alpha/多源的项目走对应节点/转场接口。
- LUT 烘出单独判定：仅在输入工作域/采样范围冻结、且算法只依赖当前 RGB 的已支持链段尝试；空间采样、mask、时间/seed、多源及 alpha 依赖不作为普通 3D LUT 可表达效果。实际 LUT 消费者对独立色块/域边界验误差；超出 LUT 域的负值/超白行为明确。选择链含不支持项时准确诊断，不能跳过后导出一份看似成功的 LUT。

### 9.2 转场特有

- p≤0 精确还原 A，p≥1 精确还原 B；测 0/1/2 帧及奇偶时长、start/center/end 对齐。中段不用“等源必须恒等”约束有意发光/形变的效果。
- 检查非零源入点、handles 不足、反放/冻结/速度曲线、混 fps/VFR、嵌套/代理；由现有时间映射供帧，不读负帧/旧镜头/错误源。
- 通过 AddTransition/UpdateTransition 等现有命令拖放/双击/编辑/移动/修剪/删除；锁轨零写入、一次 Undo/Redo、保存重开一致。
- 同一算法 CPU oracle→实际 GL 预览/导出→已实现 Metal 子集逐帧对拍；缺后端不能静默替换为 Cross Dissolve。视频效果不能意外改变 PCM。

### 9.3 节点/效果链/缓存

- 每个声称支持页面至少穿过真实入口、真实上游调色/效果链、输出消费者；防止插件重新采样原图覆盖上游结果。
- 参数动画、表达式/多输出引用、缺输入、禁用/旁路、旧工程/未知字段；端口名/类型/资源不能在导入宏后串线。
- 节点按实际 bbox/ROI/padding 验整帧对分块；全局连通域/距离场未做等价 tile 算法时声明 full-frame 并受预算约束。
- 缓存身份包含上游/拓扑、参数/版本/资源 hash、分辨率/工作域、seed/frame 等实际依赖。随机 seek、反放、重复渲染和冷热缓存一致。
- 辅助 map/纹理以同一固定资源验证 CPU 参考、实际 GL 及声明的 Metal 消费；替换资源后缓存失效，缺图/解码失败准确拒绝，不用黑纹理或恒等透传冒充成功。每个 context 独立创建并释放 GPU 资源。
- 参考 oracle 通过仅表明数学参考可用；生产 CPU 导出只有接入实际调度和消费者后才能标支持。

### 9.4 性能与资源（L21）

- 固定夹具、同源帧、同版本驱动/工作域，记录单项 1080p/4K 的 GPU/CPU 时间、p50/p95、峰值显存/内存、额外 pass/采样数；冷启动/热缓存分开。
- 同测实际 4K 时间线：单效果/5 层叠加、多轨交叠/切点、参数拖动/seek 风暴、后台导出。以 M/W 基线对比；60FPS 是整条管线目标，不给每个昂贵效果空口保证。
- 低/中/高质量必须明确其采样/分辨率差异；交付默认语义不被预览降档悄悄改变。超过预算的效果给成本标签，不能伪造“实时”。
- GL 循环上限为常量，采样/纹理池/异步队列有界；取消、shader 编译失败、设备/上下文变化、窗口关闭都释放资源并拒收过时结果。
- 先做单项成本和短时压力；有资源增长/新失败再扩大浸泡。不要无新证据反复跑全量测试消耗配额。

## 10. 发布、兼容与证据（L20–L23）

每批 3–5 项，至少交付：规格＋实际实现/适配＋独立预期＋focused 测试＋真实样片/性能＋用户说明＋主集成者的小补丁/提交列表。单包可发布，尚未验收的候选不进入可用菜单。

| 状态 | 允许的声明 |
|---|---|
| planned | 已有规格/任务，尚无实现 |
| component_ready | 暂存算法、参考、局部测试通过；仍非产品功能 |
| integrated | 已接宿主并真实运行；尚未完成所有声明平台/发布门 |
| validated | 本条声明的页面/后端/模式已有对应证据；其他矩阵仍 unsupported/untested |
| released | 本条发布支持集通过 L20/L21/L22，干净安装包可用 |
| deferred / reused | 记录原因/已有替代/关联任务；不能合并成“新增已完成” |

“算法存在”“类可注册”“工程可保存”“测试跳过”“漂亮 GIF”均不能替代真实像素、交互及导出。

L20 为每个发布 ID 建能力矩阵：页面×预览/视频导出/LUT 烘出×后端×静态/动画×RGBA/HDR×整帧/瓦片。证据包含 commit、机器/环境、真实路径/命令、结果、输入/输出 hash、误差阈值依据；不能从少数样本外推全库通过。

L22 验干净配置与冻结包、任意工作目录启动、只读安装目录、无网络、缺插件/损坏包/未知版本/缺 map、同名大小写冲突、旧工程升级后 Undo/保存重开、Mac↔Windows 离线往返。保留未知载荷并明确诊断；生产发布使用既有事务/归档机制。

L23 交 3 个最小示例工程：剪辑滤镜＋新转场；工作域明确的调色＋Look；含透明边缘、多输入、多输出的合成图。示例源自可再分发小夹具，包含重开和最终解码像素证据。目录记录真实可用数量、复用数量、延期清单和限制。

## 11. ZCode 可直接领取的工作方式

首次建议领取 L00/L01，随后只领取六个试点中的一个具体子项。接口通过后按 L10–L18 的 3–5 项小组继续。不要一次发送“实现全部 59 项”后等待巨型补丁。

可复制的首个任务：

    在 Topos Color（PyQt6 + ModernGL + GLSL，HDR float16/32）执行
    docs/upgrade_plan_L_content_library_2026-10-24.md 的 L00/L01，
    只为试点 F16 GradientMap 准备第一份可审阅组件交付。
    先解析本机仓库根并在报告中使用绝对路径；遵循 AGENTS.md/.clinerules。
    阅读现有 Duotone_Toner、PluginManager、EffectCatalog、dctl_wrapper、
    Composite ParamDef 与既有 plugin focused tests，核实已有差量和参数 ABI。
    仅写 content_staging/library_v1/、tests/content_library_v1/、
    docs/content_library_v1/、docs/validation/upgrade_L/ 内自有文件。
    不改生产 plugins、共享 registry/renderer/schema/i18n/依赖/父 conftest，
    不改 M/W/T 计划状态，不覆盖其他人的未提交内容。
    交付：参数/域/alpha/neutral/能力规格、独立 CPU 数学参考、
    点处理源码草稿、手算与 HDR/透明夹具、局部验证结果，
    以及给 Mac 集成者的最小适配提案。生产跨页/GPU/导出无证据时标未验。
    若四个色标不能由当前参数协议表达，先明确原生适配契约，
    不静默减少成另一份 Duotone，不伪造完成。

每次回收时要求简短报告：本次实际变更、执行命令/结果、已验支持集、未验/阻塞与对应主责、下一个具体 ID。Token 用于独立审查、反例/边界、参数质量、样片制作与下一项实现；没有必要用无限重跑消耗额度。剩余额度/到期/并发以实际服务商账户为准，本计划不作 Token 数量或速度承诺。

## 12. 源码与测试起点

以下为已存在的定位入口，开工按符号重查当时版本，不把行号当固定接口：

- Color：src/features/color/core/plugin_manager.py；docs/PLUGIN_DEV_GUIDE.md；plugins/；docs/plugins/2026-09-28/。
- Shared：src/features/shared/effects/effect_catalog.py、dctl_wrapper.py、plugin_identity.py。
- 转场：src/features/edit/core/transition_catalog.py、custom_transitions.py、transition_mask.py、transition_effects.py、transition_mask_gpu.py、gpu_transition_shaders.py、timeline_metal_export_renderer.py。
- 合成：src/features/composite/core/node_plugin.py、node_engine.py、node_capabilities.py；nodes/base_node.py；evaluators/base_evaluator.py；render/composite_export_renderer.py。
- 预设：src/features/color/ui/presets_panel.py、core/preset_meta.py；src/features/composite/utils/preset_manager.py；Edit custom_transitions.py。
- 滤镜测试：tests/plugins/test_plugin_batch1.py、test_plugin_batch2.py；tests/color/test_builtin_plugin_compile.py；tests/shared/test_dctl_and_preview.py；tests/edit/test_effect_catalog_capabilities.py。
- 转场测试：tests/edit/test_native_transitions.py、test_transition_effects.py、test_transition_mask_gpu.py、test_gpu_transition_preview.py、test_custom_transitions.py、test_transition_and_slide_transactions.py、test_audio_transitions.py、test_transition_preview_mode.py。
- 合成预设：tests/composite/test_composite_preset.py；Color 预设：tests/color/test_presets_gallery.py。

已有测试作为参考与回归起点，不承诺它们现在都通过；本轮没有执行这些产品测试。实施时用项目实际解释器运行与变更相关的 focused tests，再补真实消费者门。

### 方向参考

截至 2026-10-24 查阅的官方页面：[DaVinci Resolve Edit](https://www.blackmagicdesign.com/products/davinciresolve/edit)、[Color](https://www.blackmagicdesign.com/products/davinciresolve/color)、[Fusion](https://www.blackmagicdesign.com/products/davinciresolve/fusion)。用于参考剪辑效果、调色和节点合成的产品方向；本计划的数量、缺口与实现路径来自本仓库盘点，不由竞品页面推断。

本轮不包含第三方商业插件代码/素材复制、完整 OFX 宿主、新 AI 模型平台、通用 3D 引擎或新历史帧调度器；这些如后续立项，分别明确 SDK、许可、资源与性能条件。当前 L 线先把可复用的单帧 2D 内容及已有工作流做成真实可交付的库。

