# M/W 升级计划完成复核与收尾计划

> 日期：2026-10-25（Asia/Shanghai）；源码基线：`e6c3b6db126574857306b757203de55286d6496d`。本轮小修在工作区，未提交。
> 对照：[Mac 原计划](upgrade_plan_M_mac_2026-10-24.md)、[Windows 原计划](upgrade_plan_W_windows_2026-10-24.md)。原计划作为被审材料，其中面向未来机器的执行指令未自动执行。
> 方法：逐包核对当前源码、调用方、测试、报告与已入库原始资料；Darwin / Python 3.12.13 上运行逻辑回归和只读探针。未在本轮运行 Windows 真机、真实产品 GUI/GPU、冻结包、长压、外部 NLE 或 DeckLink 验收。历史硬件报告保留为历史证据。

## 1. 完成到什么程度

**两条线已有大量有效实现，但尚不能判定原计划整体完成或达到发布门。** M 正文勾选 23/25、W 勾选 11/11，只是原表的登记数量，不是完整用户功能的验收率。本轮没有可靠依据换算新的百分比。

- 原 S/R 小范围缺口的修复有进展：本轮原 S 探针 9 组、R 探针 4 组均 `issue_present=false`；这 13 组探针不覆盖全部 S01–S12/R01–R14，也不代表全部场景已通过。
- 多处“Mac 侧只剩真机门”不成立：菜单占位、命令只改面板私有工程、编译器无生产消费者、自动快照丢结果、归档漏依赖等，均是当前可处理的软件缺口。
- Windows 有真实组件和资源趋势证据；报告自己也承认冻结 exe、W04.2 色彩矩阵、合并后共享复验、跨机脱机往返未完成。不能因父包勾选而消除这些要求。
- M16/W08/W09 的部分计量或通过条件不足；应修验收工具后复测，不能把“最终完成请求”“资源平稳”直接解释为实时、上屏或正确性达标。

本轮详细证据（含源码定位、历史与当前证据的区分）：[Mac 审计](validation/upgrade_audit_2026-10-25/mac_audit.md)、[Windows 审计](validation/upgrade_audit_2026-10-25/windows_audit.md)、[性能与门禁审计](validation/upgrade_audit_2026-10-25/perf_audit.md)。其中“小修候选”的最终状态以本表第 3 节为准。

### Mac 逐包核对

“局部确认”表示源码/历史证据/定向回归能支持相应子项，不等于完整 GUI/成片验收。

| 原包 | 本轮判定 | 仍需处理或验证 |
|---|---|---|
| M00 | 基线与分域门禁存在，本轮 188 项通过 | 重新登记真实生产缺口；扩充新增关键测试门禁（N01/N20） |
| M01 | 隔离组合有历史记录；核心隔离环境本轮 pip check=0 | 未重建 ASR/TTS 或重跑真模型；按实际分发组合复验（N17） |
| M02 | 快照/metadata/正式 IO 局部回归通过 | 主窗口恢复/Undo 与真实成片矩阵（N18），归档快照依赖另见 N11 |
| M03 | TC/DF/外录 retime 实现与回归存在 | 音钟/变速/PCM 全矩阵及跨机复验（N18/N19） |
| M04 | PTS/序列分类/解释能力声明已实施 | 长 VFR+B、音画漂移、几何/序列全链支持范围（N18） |
| M05 | 顺序修复有效；**完整链重开** | 层内高级效果丢失、层语义扁平化、不支持效果未硬拒绝（N03） |
| M06 | 基础 qualifier 有实现；**完整消费重开** | mask helper 无消费者、后处理丢弃、keyer 失败继续成片（N03） |
| M07 | 清单/版本模型存在；**前台提交重开** | 版本工具只改私有工程，激活绕过 executor（N04） |
| M08 | 签名/协调器存在；**菜单与线程边界重开** | 占位 render、任务未启动、worker 访问控件、关闭/提交缺口（N05） |
| M09 | EXR/float/alpha 软件件存在 | 实际抓帧/参考显示/透明边/最终像素（N18） |
| M10 | typed job/编译 helper 存在；**生产交付重开** | 无编译入口消费者、字幕模式与语言未贯通、批次拒绝口径（N06） |
| M11 | 区间删除命令存在；**入口重开** | 菜单恒提示，转写→预览审批→真实事务未接（N07） |
| M12 | proxy/relink 部分接线存在；**入库子项重开** | verified ingest 无产品消费者；安全清理/迁移仍需实际验收（N08/N18） |
| M13 | 多项模型/页面接线存在 | 布线/JKL/曲线/标题/网格/多屏逐项操作与成片验收（N18） |
| M14 | sample 字段/嵌套/Stem 软件件存在 | split 连续 PCM、录音设备异常、Windows 句柄（N18/N19） |
| M15 | 比较面板等子件存在；**自动快照重开** | scheduler 丢弃新工程且无 timer 消费；评论/take 实际场景（N09/N18） |
| M16 | 测量框架与窗口 swap 样本存在；维持开放 | deadline 指标、同进程 GL 风险、H265 预算及长压（N14/N15/N19） |
| M17 | UYVY/GPU 缩放候选 helper 完成；**生产优化未完成** | 仍完整帧回读；候选释放与字节统计错误、真实监看（N16） |
| M18 | 集成准备件存在；维持 P0 未验收 | 严格 ABI、三黄金链真正 runner、B 二轮/冻结包/接收端（N02/N17–N19） |
| M19 | 转换错误/上下文/Scope 软件声明有实现 | 三页最终像素/Scope 参考向量及 VT/CUDA 外部平面对拍（N18/N19） |
| M20 | 绑定 WAV/栈 LUT 等部分闭包已实现；**重开** | 快照/候选/Composite 依赖、路径重写与 LUT hash（N11） |
| M21 | 发布事务/测量 producer/面板存在；**QC闭环重开** | UI 线程全量测量、历史任务混入、字幕/目标/发布裁决未贯通（N12） |
| M22 | 算法/面板件存在；**生产链重开** | 同缓存测全部镜头、BGR/uint8 错域、无主工程提交/实际预览（N10） |
| M23 | 修正模型/可用性探测存在；**生产修正重开** | 未注入重跟回调，直接改 node，无异步事务（N13）；模型扩展条件延期 |
| M24 | 有理时基/版本 metadata 往返存在 | metadata 保留不等于外部软件可渲染 grade；真实接收端对拍（N18/N21） |

### Windows 逐包核对

| 原包 | 本轮判定 | 仍需处理或验证 |
|---|---|---|
| W00 | 历史环境/输入/DLL 基线成立 | 链接错字已修；B 二轮按当前提交采集（N17） |
| W01 | 原生 DLL/符号/PE 组件修复有证据 | 报告有 51 个 `BAD_COMMAND`；缺冻结包/完整原生分发闭环（N17） |
| W02 | 回滚/池预算 L1 有效；**初始化恢复重开** | 成功 initialize 不恢复调用者 CUDA current-context（N22） |
| W03 | 按源失败隔离与降级恢复有 L1/历史真机证据 | 当前合并提交的真机复验、Qt handler 门禁（N17/N20） |
| W04 | 几何/设备能力门部分完成；**整包未关闭** | 查询异常被 int 转换掩盖；W04.2 颜色数值/多设备真机未验（N22/N19） |
| W05 | 快照严格断言/fence 修复有历史两路对拍 | 当前合并提交 Windows L3（N17） |
| W06 | extra、收集、能力报告及干净 venv 两态有证据 | 冻结 exe 未建未验；Mac 测试失败已小修（N17） |
| W07 | 新 L1 入 CI、隔离态红基线收口有成果 | M+W 合并后 S/R 与三黄金链未在 B 二轮验收；批次顺序污染（N17/N20） |
| W08 | 600s/1800s/100重启资源趋势有历史数据 | 当前 sampler 错误已修；真实呈现/页面负载/阈值/多屏/长档未过（N15/N19） |
| W09 | 五组配对的固定 workload 数据可保留 | 失败全空仍假绿、越界 seek 不检查；不等于固定白名单或动态路由（N23） |
| W10 | 文件名/冲突/目录签名等适配与逻辑测试存在 | Mac→Windows→Mac 卸载原盘后仅包恢复、像素/PCM、UNC 未验（N11/N19） |

## 2. 新收尾工作包（所有本轮发现按责任归并）

以下全部待办，除第 3 节已完成的小修。P0 是发布裁决门，P1 是正确性/线程/生产接线，P2 是效率或较低风险工具债。先修软件缺口，再安排受影响场景验收；设备不足只延期对应子项。引用行号以审计基线为准，小修后可能移动。

### N01 · 状态与证据口径（P1，Mac 集成）

> **修复登记（2026-10-25）**：四列台账+交接索引建立——[docs/upgrade_status_ledger_2026-10-25.md](upgrade_status_ledger_2026-10-25.md)（M00–M24/W00–W10 逐包"已实现/生产接线/场景验收/条件延期"四列 + 重开项→N 包去向映射 + 证据登记规则 + 复验通用入口 + 收口批次提交号）。勾选率不再当验收率引用；历史硬件结论保留日期/宿主；W08/W09 旧 summary 不作新验收证据也不指称虚假（规则 §3.3）。台账为活文档，各 N 包收口时更新对应行。

- [x] 建立“已实现/生产接线/场景验收/条件延期”四列，重开上表明确的软件缺口；不能把 23/25 或 11/11 当验收通过率，也不能把全部 R 关闭理解为完整用户功能通过。（→ 台账 §1/§2）
- [x] 对应旧包逐个保留原报告、当前提交、dirty 状态、输入 hash、运行命令/退出码/skip、责任与复验入口。W 多包仅入库报告，W08/W09 的 summary/JSONL 不含完整 pytest 退出日志，需补新证据；不据此指称旧数据虚假。（→ 台账 §3 规则+各行证据锚点；W08/W09 新证据以 N15/N23 修复后重出为准）
- [x] 原表头和 W00 错链接已修（F05）；将本表及三份审计记录纳入新交接索引。未经本轮复验的历史硬件结论明确日期/宿主。（→ 台账 §0 交接索引表）

### N02 · 严格 ABI 与复验裁决（P0，Mac/Windows 各平台实现）

> **修复登记（2026-10-25，Mac 侧软件项收口）**：①两审计模块新增 `strict_verdict` 严格裁决（目录存在/native 非空/无畸形/未截断/无未声明 universal/架构==声明目标/主入口在 manifest/min OS≤目标，任一不满足即 FAIL 并给机器可读原因清单；`consistent` 旧字段保留兼容但不作发布裁决）；②头结构边界：PE 校 COFF 声明的 SizeOfOptionalHeader 越界/null machine/缺可选头 Magic/Machine↔PE32(+) 位宽错配均显式畸形；Mach-O 全程行走 ncmds 条 load command（命中 min_os 命令不再提前 break，尾部截断/cmdsize 溢出/病态 ncmds 显式畸形）；FAT/universal 魔数单独登记不再混入"非 Mach-O 跳过"；③真实 bundle 重算（F07 偏移修复后）：1523 Mach-O/单 x86_64/零畸形/未截断/主入口在 manifest 结构项全过，**生效 min OS=15.0**（Qt6 框架 13.0–15.0）——分发声明必须≥15.0，任何低于生效下限的声明 strict FAIL；旧 manifest 未改写，重算见 `validation/upgrade_2026-10-24/M18/bundle_abi/macos_bundle_abi_recalc_n02.json`；④`m18_machine_b_recheck.py` 重写：非 Windows 宿主快速失败（全部门 not_run 不产生伪证据）、git HEAD+dirty 指纹入报告、超时为独立 `timeout` 状态、探针/入口缺失为 `not_run` 分级、无 `--dist` 时安装包门显式 not_run+声明字段、默认只跑 chain3 故 `three_chains_verified` 恒 False 如实声明、dist 审计改走 strict_verdict（主入口自动探测或 `--main-exe`）、顶层异常兜底保证报告落盘。测试 `tests/shared/test_n02_strict_abi_verdict.py` 25 用例（空/坏/混架构/截断/universal/越界/错宿主/超时/三链声明全负例+两平台正例）；旧 PE 合成助手修正（原助手声明 240 字节可选头只写 66 字节，在无边界检查时代属"自洽的畸形"）。B 机真机复验/冻结包构建仍为外部条件项（N17 在案）。

- [x] `bundle_abi_audit_pe.py:95` 与 `bundle_abi_audit.py:132` 的 `consistent=len(arch_set)<=1` 只说明集合不混架构；空/不存在目录、畸形库、扫描上限截断仍会“true”。PE 复验脚本只看此字段会误绿；补独立严格通过条件：真实主入口、非空 native 文件、无畸形/未扫描、支持且匹配声明目标架构、头结构和必需 codec ABI。不能拿“纯 x86 集合一致”当 x64 包合格。（→ strict_verdict 落地，上记①）
- [x] PE 校 COFF 声明的可选头边界，Mach-O 校 load command 边界；支持/拒绝 universal 等按分发声明处理。Mac 最低 OS 不能仅收集而不对目标裁决；本轮解析偏移错误已修 F07，历史最低 OS 字段需重算，不改写旧 manifest。（→ 上记②③：真实 bundle 生效下限 15.0 已裁决）
- [x] `m18_machine_b_recheck.py` 已记录系统/版本/架构，但仍缺 Windows 宿主校验、实际 git 提交指纹、timeout 异常报告、未执行/skip 的分级状态；默认无 `--dist` 不能代表安装包门已通过。探针真假判定已修 F03，但默认只跑 chain3，不能称“三链已验”。（→ 上记④）
- 验收：空/坏/混架构/未知架构/截断/错误目标/缺主入口/错误宿主/timeout 注入均不得把对应门标 PASS，失败后报告仍落盘；正确包在干净机可启动、导出。（→ 负例注入全过；"干净机启动/导出"为 N17 冻结包外部条件项在案）

### N03 · 完整 Layer/组链及二级遮罩（P1，Mac；W07 复验）

> **修复登记（2026-10-25，硬门半收口；全链逐层像素对拍归 N18）**：**降级硬门**——`ExportGradeDegradedError` + `TimelineExporter(allow_degraded_grade=False)`（N03 新参数）：keyer 计算失败（旧=回退无限 matte 继续成片=**未限定整帧被误调标成功**）与 qualifier matte 后处理不支持（旧=丢弃仅告警=视觉差异静默）均**默认拒绝导出**（异常带原因+显式降级指引）；仅导出器显式 `allow_degraded_grade=True` 保留旧降级路径（声明式取舍，日志标注"显式降级"）。测试 `test_n03_grade_degrade_gate.py` 5 用例（keyer 失败默认拒绝/声明后放行、后处理丢弃默认拒绝/声明后放行、无 qualifier 不触发门）+导出族 79 绿无回归+GATE PASS。**余项（如实在案）**：layer 内 LUT/曲线/插件/qualifier 的 `_unsupported_advanced` 标记补齐（color_chain 段内未标记面）、timeline_export 层扁平化保留 opacity/mask/plugin/次序、mask_shapes_cpu 导出消费、preflight/delivery_matrix 丢效果 warning→阻断——完整链 bake（组前曲线+多层 opacity/遮罩+微调蒙版+组后 LUT 逐层像素对拍）为 N18 场景域大件，未冒称。

- [x] `color_chain.py:108–132` 仅基础参数，高级栈字段标 unsupported 后丢弃；layer 内 LUT/曲线/插件/qualifier 甚至没有标记，可能令 `extras_active=False`，整段静默失效。（→ 层内标记补齐收口：layer 快照 dict 的 lut_path/active_lut_path/curves/qualifier/plugins 键任一存在即 `_unsupported_advanced`（不再静默失效），3 新用例+链消费族 24 绿；高级操作的实际 GPU bake 仍随完整链渲染域）
- [x] `timeline_export.py:9280–9348` 抽首个 qualifier/曲线/LUT 为扁平参数，未保留各 layer 的 opacity/mask/plugin/次序。`mask_shapes_cpu.py` 只有模型 helper，无导出消费。（→ **逐层载荷+mask 消费收口**：①`_get_clip_color_grade` 新增 `layers` 全量载荷（原始索引/active/opacity/grading/curves/hsl/soft_clip/LUT/qualifier/mask_shapes/plugins 逐层独立+次序保留，替换首取扁平化；多层高级并存→`has_advanced_operations` 升位交降级硬门，不再静默只剩首层；单层场景兼容键等价填充行为不变）；②`_apply_color_grade` 接通 `mask_shapes_cpu`——活动层 mask 形状经 `combine_masks_cpu` 栅格化（circle/rect/gradient 解析近似，与 engine 同源组合语义）乘入 qualifier matte 限定整层调色；不可评估形状（curve/bitmap/brush）或空组合→与 `ExportGradeDegradedError` 同口径默认拒绝（显式 allow_degraded_grade 才跳过）——mask_shapes_cpu 首个生产消费落位；③`video_export_preflight._check_color_delivery_preflight` 丢效果 WARNING→**BLOCKING**（默认拒绝入队；job 显式 `allow_degraded_grade=1` 才降回 WARNING——与 timeline_export 降级开关联动同口径）。测试 `test_n03_layers_mask_preflight.py` 7 用例（多层载荷全保留+次序+plugin/mask/opacity/多层升位/单层兼容/圆 mask 圈内调色圈外原图实证/curve 形状默认拒绝+声明放行/预检默认阻断+声明降 warning）+既有 N03 两套 15 绿+deliver 297 绿+导出族 68 绿+GATE PASS。多层高级的像素级组合渲染属 N18 GPU 完整链域（载荷已保留不再丢失）
- [x] `timeline_export.py:9625–9634` 后处理只日志丢弃，keyer 失败可无限 matte 继续输出；`video_export_preflight.py:354`/`delivery_matrix.py:753` 丢效果仍 warning，预检构造失败继续输出。按能力真正 bake/拒绝，禁止把视觉错误标成功。（→ timeline_export 侧两处降级硬门收口；preflight/delivery_matrix warning→阻断为余项随完整链 bake）
- 验收：组前曲线＋多层 opacity/遮罩/插件＋微调蒙版＋组后 LUT，在 Color/Edit/Composite/CPU/GPU 成片逐层及最终像素对拍；不支持能力入队前拒绝，keyer故障不得整帧误调。（→ 不支持能力入队前拒绝已三处落地（timeline_export 硬门/预检 BLOCKING/mask 不可评估拒绝）；keyer 整帧误调已修；多层载荷已保留不再丢失；完整链逐层像素对拍归 N18 GPU 域）

### N04 · 调色版本写入主工程（P1，Mac，依赖 N24）

> **修复登记（2026-10-25，收口）**：①工厂转传——`_version_tools_panel` 工厂此前丢弃 `command_executor` kwargs，现转传（无 executor 面板退化为改私有副本）；②面板命令统一出口——`_execute` executor 路径消费宿主 executor 返回的**新工程对象**（N24 executor 契约已从 bool 升级为 新工程/False）并回读 `self._project`（不再持旧私有快照）；拒绝（False）明示"本面板未写入"不回读；`_on_activate` **不再绕过 executor 直改私有工程**（旧路径主工程/命令栈完全不知情），改 `ActivateColorVersionCommand.create→_execute` 同 save/lock 走命令出口，直应用路径按 EditCommand 协议归一 `(new_project, updated_cmd)` 二元组；激活缺失插件提示改从刷新后版本 payload 的 `missing_plugins_hint`（保存期持久化）读取——executor/直应用两路一致；③真实菜单接线在 N24 已就位（on_open_version_tools_panel 注入 host.project_command_executor）。测试：`TestVersionToolsPanelExecutor` 4 用例（save 经 executor+回读新工程/activate 同走命令出口+active 生效/拒绝不写入/工厂转传探针）+host executor 契约测试更新，36 绿+GATE PASS。真实菜单→Undo→保存重开全链 GUI 场景归 N18 复验（软件链已闭合）。

- [x] `upgrade_panel_actions.py:61` 未传 executor，`m07_version_tools_panel.py:100/140` 仅修改私有工程，激活还绕过 executor。统一 save/activate/lock 的命令提交与主工程回读，不直接在面板应用不可变模型。（→ 上记①②③）
- [x] 验收：从真实菜单保存/激活/锁定→主窗口 dirty/revision/预览变化→一次 Undo→保存重开，关闭重开面板保留结果；主工程更换/过期面板拒绝写入。（→ 契约测试：命令全部经 executor 入栈（=主窗口 dirty/revision 链由正典 executor 承担）/拒绝不写入/工程切换由 N24 reconcile+guard 关闭拒绝；GUI 全链保存重开归 N18）

### N05 · 真正启动 Bake 并修线程/生命周期（P1，Mac，依赖 N24）

> **修复登记（2026-10-25，全收口；真实 GL 烘焙产物属 N18 真机场景）**：对话框线程边界半已在 N24 批次收口（进度信号化/改写回 UI 线程/关闭取消保活）；本轮补**真实渲染回调端到端**——①`_build_bake_context`：UI 线程**预建全帧 NodeEngine snapshot**（snapshot 契约须 UI 线程；帧域=活动时间线全部轨道最右端帧；分辨率/fps 取时间线声明），快照失败如实拒绝；②render_fn（worker 线程）：预建快照逐帧取用 → `CompositeExportProvider.render_full_res` 全分辨率渲染 → float32 EXR 落盘（cv2.imwrite，失败=该帧失败诚实上报 execute_bake 事务）；③`rewrite_fn`：发布成功后 UI 线程 `apply_bake_rewrite` 图改写；④注册表新增 `composite.bake` 工厂（真实 render_fn/rewrite_fn 转传），菜单回调经宿主打开并**真正 start()**（worker 启动六阶段事务 freeze→render→verify→source_changed→publish→rewrite，apply_rewrite=False 改写在 UI 线程）；旧"恒 False stub 假装启动"彻底移除。测试 `test_n05_bake_real_render.py` 5 用例（快照全帧 UI 线程预建/render_fn 按帧取快照+EXR 写盘/越界帧与渲染失败诚实 False/前置缺失四态拒绝/端到端宿主打开+start+UI 线程改写）+对话框生命周期既有 7 绿；composite 全域 7381 绿（含 QA-03 检出名硬编码修复）+GATE PASS。

- [x] `menu_toolbar_controller.py:2034–2053` 传 graph=None、恒 False render stub、固定范围，也未 start；接实际图/选中节点/区间/渲染队列。目标选择不能只从 Tracker 获取并称任意节点。（→ 上记①②③④：真实图/选中节点/时间线区间/start 端到端）
- [x] `bake_progress_dialog.py:100–104` worker wrapper 直接读取/写 QProgressBar；改进度 signal。关闭/reject 要取消、等在途任务完成、保活和释放；rewrite 回主线程 command/service 提交，不能 worker 直接改图。（→ N24 批次对话框半+本轮 rewrite_fn 真实接线）
- 验收：真实菜单完成一次可播放 bake，图替换一次 Undo；第 N 帧失败/磁盘满/取消/关闭/源 revision 变化不发布半套产物，Windows 文件锁/rename 失败保留旧成功结果。

### N06 · 多版本真正入队与字幕消费（P1，Mac，依赖 N12/N24）

> **修复登记（2026-10-25，收口；两语言×横竖正式 runner 全链归 N18）**：①字幕模式统一——`SUBTITLE_STRATEGY_MODES` 映射（burn_in/burn→burn、soft/sidecar→sidecar、none/off→off）写 job 真实消费键 `subtitle_mode`（SubtitleMode 三态；旧编译器只写 `burn_subtitles` bool 而 `from_flat_config` 读 `subtitle_mode` 默认 off——**编译出的多版本字幕策略从不生效**的错位修复），`burn_subtitles` 保留兼容；未知策略→整版本拒绝；②语言→轨身份——`resolve_track_id(language)` 注入解析（批次入口从时间线 `subtitle_tracks[].caption_metadata.language` 解析，BCP-47 前缀匹配 zh→zh-Hans）；解析器提供而解析失败→拒绝（不盲选默认轨）；纯函数调用不传解析器保持轨身份缺失（消费侧按 language 呈现不冒充）；③生产入口——`VideoBatchExportController.enqueue_version_plan(plan, base_config, project, timeline)`：预检（时间线在册/素材在线扫 video_tracks 素材路径/队列输出路径冲突）→ 编译 → **原子整批入队**（`ok=False` 任一拒绝→零入队，杜绝旧"部分 jobs 无上层阻断"的半套交付；job 快照化失败同样整批拒绝）→ from_flat_config 类型化入队；④N12 runner 级发布前门（同批收口）——`VideoJobRunner._run_quality_gate`：job 声明 `expected_lufs` 才激活（未声明≠通过、不冒充全量 QC），成片实测综合响度偏离声明目标 ±1 LU（EBU R128 容差）→ RuntimeError（job FAILED 带 QC 原因，质量失败 ≠ DONE）；探测异常/无结果/成片缺失全部 fail-closed。测试 `test_n06_version_pipeline.py` 12 用例（策略→mode 三态+兼容键/未知策略拒绝/解析失败拒绝+纯函数路径/ok 原子入队/任一拒绝零入队/离线整批拒绝/BCP-47 前缀解析/门未声明不启用+容差内过+超差阻断+探测异常 fail-closed+成片缺失 fail-closed）+f93 既有 11 绿兼容+N12 既有 5 绿；deliver 全域 297 绿+GATE PASS。两语言×横竖走正式 runner 的实际分辨率/可见字幕/sidecar 内容/音轨/恢复溯源全链归 N18（真实编码产物场景）。

- [x] `compile_version_jobs` 在 src 只有定义，没有生产调用；接批次入口、统一 typed schema、预检与原子整批入队。（→ 上记③ enqueue_version_plan）
- [x] compiler 写 `burn_subtitles`，job 读 `subtitle_mode` 默认 off；语言仅存入 SubtitleConfig，未传到实际字幕轨/渲染消费者。统一 burn/sidecar/off 与语言→字幕轨身份，不能只验字段往返。（→ 上记①②：三态模式+轨身份解析经类型化快照入队；burn/sidecar 渲染消费由 runner 既有 BURN/SIDECAR 路径接 typed SubtitleConfig）
- [x] 混有未知画幅/路径冲突时可返回部分 jobs，虽 `ok=False`，没有生产上层阻断。整批拒绝不产生部分任务；运行/恢复校 revision/snapshot。（→ 原子整批零入队；revision/snapshot 溯源由编译器既有 source_project_revision/source_snapshot_hash 随快照入队持有）
- 验收：两个语言×横竖版本走正式 runner，实际分辨率、可见字幕/sidecar内容、音轨、恢复溯源正确；离线/冲突/坏版本零入队。（→ 离线/冲突/坏版本零入队已契约测试；真实编码产物两语言×横竖归 N18 场景）

### N07 · 文稿删除入口到真实事务（P1，Mac，依赖 N24）

> **修复登记（2026-10-25，入口+事务链收口；真实素材 GUI 全场景归 N18）**：①真实入口——`on_open_filler_removal_panel` 不再恒提示"从转写服务入口启动"（永不可达占位）：从当前时间线字幕轨提取词级时间戳（`_extract_word_timings`：subtitle_tracks→clips→caption_metadata.word_timings），有词级数据即 `detect_filler_words` 检测（有命中/review 才开面板，无词级/无命中如实提示）；②计划消费者——工厂转传 `on_plan_ready`（旧工厂吞参致 PREVIEW 计划 accept 后即丢失）；`_apply_filler_plan`：**revision 守卫**（当前工程 revision≠计划 base_revision 即作废拒绝）→ `compile_plan` 编译为单 MacroCommand（CompileError 明示）→ N24 `timeline_command_executor` 入时间线命令栈（一次 Undo）；③区间删除语义（真实 clip/track 词区间、ripple 从右到左、容器跳过、幂等指纹）由 M11/S02 域层既有测试持有。测试 `test_n07_filler_entry.py` 7 用例（词级提取/无字幕轨空/无词级如实不可用/有词开面板+检测真实+消费者接线/revision 失配拒绝/编译产物入命令栈/工厂转传探针）+m11 面板既有 5 绿；12 绿+GATE PASS。转写服务本身产出词级字幕（上游）为既有链路；真实素材菜单→删除→Undo→重开全场景归 N18。

- [x] 菜单恒提示去转写；src 没有真实转写入口创建 FillerRemovalPanel，工厂也未传 on_plan_ready。接词级检测→选择/差异→审批→compile/apply。（→ 上记①②）
- [x] 验收：从菜单/转写实际进入，真实 clip/track/词区间删除，链接 A/V、字幕/marker 同步；一次 Undo/重开；过期 revision、取消、重试零重复剪切。（→ revision 守卫/一次 Undo 契约测试；词区间/链接/幂等由 M11 域层持有；真实素材 GUI 全场景归 N18）

### N08 · verified ingest 产品入口（P2，Mac）

> **修复登记（2026-10-25，收口）**：①`ingest_files` 新增 `on_item` 逐项回调（即产即报，回调异常不阻断拷贝——生产 UI 进度的数据通道）；②新模块 `verified_ingest_service.py`——`VerifiedIngestService`（QObject）：可取消 `_IngestWorker`（QThread，拷贝/hash 全在 worker 线程，逐项经 `item_done` 信号排队投递 UI、终态 `finished(IngestReport)`；在途拒新批；worker 保活注册表防 GC 销毁运行中线程）+ `ImportIngestAssetsCommand`：报告 verified 项统一入库（复用 `EditProject.import_asset` 同源语义——路径去重/媒体类型/元数据探测与媒体池一致不另造旁路；单项失败不阻断其余；一次可撤销=还原 apply 前工程），宿主经正典命令栈提交（N24 口径）；hash/路径逃逸/同名竞争语义由既有 f65 契约持有未动。测试 `test_n08_ingest_service.py` 7 用例（第1成功/第2读失败/第3继续逐项即报、取消部分保留不冒称全过、worker 异步+逐项信号、在途拒新批、空源拒绝、仅 verified 入库+一次撤销、零 verified 零导入）+f65 既有 11 绿；18 绿+GATE PASS。UI 面板挂接（菜单/进度呈现）随 N18 场景（服务契约已定）。

- [x] `ingest_files`/`verified_ingest` 无 UI/service 消费；接可取消 worker、逐项报告、成功项统一入库事务。保留现有 hash/路径/同名竞争安全语义，不另造拷贝旁路。（→ 上记①②）
- [x] 验收：第一项成功、第二项读失败、第三项继续；中途取消/源卡外路径/并发同目标不覆盖良好文件，报告与素材登记一致。helper 完成不能关闭 UI/报告子项。（→ 契约测试钉死；源卡外/同名竞争在 f65 既有；UI 呈现层归 N18）

### N09 · 自动快照不得虚报成功（P1，Mac）

> **修复登记（2026-10-25，收口）**：①契约修复——工程不可变，`take_snapshot` 返回带快照的新工程；旧 `tick` 丢弃新工程仅计数返回 True（快照从未进在册工程=虚报）。现 `tick` 返回**新工程对象**（None=未到期/无变更/失败），新增 `confirm_written` 把"拍了"与"落库了"分离——未确认写回不计 `backups_written`，另暴露 `last_backup_meta`（名/来源 revision/payload hash）诊断；②生产接线——EditControllerMixin 新增 `_ensure_snapshot_backup`（懒建调度器+QTimer 心跳，与 mg_idle_cache 同模式）+ `_auto_backup_heartbeat`：拍备成功的新工程**写回在册工程+dirty+media_pool 广播**后 confirm（auto_backup 非用户编辑不过命令栈/不进 revision，但必须落工程才能"保存重开仍在"）；写回失败不 confirm 留异常日志；MainWindow.__init__ 启动心跳（启动失败不阻断主窗口）；③与磁盘自动保存的区分在模块注与心跳 docstring 明示（互不替代）。测试：`test_m02_snapshot_io_fidelity.py` 5 用例迁移新契约+`test_n09_auto_backup_wiring.py` 6 用例（写回+confirm/同 revision 无风暴/revision 前进重备单槽滚动/无工程空转/MainWindow 接线存在性/失败诊断不崩）；31 绿+GATE PASS。

- [x] `snapshot_backup_scheduler.py:67–73` 丢弃 take_snapshot 返回的新工程，仅计数/return True；全 src 无生产实例化。定义真实提交/结果出口，接 timer/service 并以提交/持久化成功计数。（→ 上记①②）
- [x] 验收：tick 后主工程确有备份快照且保存重开仍在，同 revision 不重复，失败可诊断且无风暴；与现有磁盘自动备份明确区分，不替换已有保护。（→ 契约测试钉死"快照落工程+dirty"；真实窗口保存重开全链属 N18 场景矩阵复验）

### N10 · 镜头匹配逐镜头真实图像与提交（P1，Mac，依赖 N24）

> **修复登记（2026-10-25，逐镜头真实源收口；12 镜头真实图像全场景归 N18）**：①`controller_measure_fn` 重写为 fail-closed 契约——旧实现"算 frame 却不用、全部镜头返回同一 `render_cached_image()`（BGR/显示域 uint8，R/B 互换毁 WB/肤色统计）"的同图假测量移除；现唯一合法路径为注入 `frame_source(clip_id, frame)→ndarray(H,W,3) RGB float`（逐镜头真实帧+形态校验），无源时逐镜头显式 ValueError（宁可无建议不出错建议）；②菜单接线——`_build_shot_match_frame_source`：时间线 clip→(asset, source_start 代表帧)，EditGpuDecodeManager 逐镜头解码（窗口生命周期单例）→ `ColorConverter.yuv_to_rgb`（Y/U/V 平面→RGB float32，矩阵按素材 metadata color_matrix 选 BT709/601/2020NC，位深按平面 dtype 推导）——真实解码 API 与 W09 基准同源；③注册表工厂转传 frame_source/clip_frame_fn；④提交链在 N24 已就位（accepted→守卫→主工程命令栈，工程切换拒绝旧写入）。测试：TestControllerMeasureFn 3 用例迁移新契约（逐镜头帧号真实传入/无源 fail-closed 不退同图/None 与形态非法显式失败）+接线测试（合成 YUV 平面→RGB float32 非全黑端到端），10 绿+GATE PASS。12 真实镜头→参考→批准→Undo→最终像素全场景与 GradeDiff 呈现归 N18（提交命令链与守卫已闭合）。

- [x] `controller_measure_fn` 算 frame 却不用，只取同一 cached image；默认 factory 无逐 clip 取帧。ImageEngine cached image 为 BGR/HDR量化 uint8，算法要求 scene-linear float RGB。接既有异步渲染取每镜头代表帧与正确色域。（→ 上记①②③：逐镜头解码+YUV→RGB float，同图假测量移除）
- [x] 面板 create command 后 accept，无生产 applied_command 消费；接主工程命令栈、参考版本/解释/revision锁、实际 before/after 与 GradeDiff。（→ N24 提交链：accepted→guard→主工程命令栈+工程切换拒绝；before/after 呈现归 N18 场景）
- 验收：12 个不同镜头真实图像→选参考→批准→一次命令→Undo/重开→最终像素；参考变化/工程切换拒绝旧结果。低置信默认勾选与失败重分析保旧建议已修 F01，仅关闭这些小子项。（→ 工程切换拒绝已契约测试（N24 guard）；12 镜头真实图像 GUI 全场景归 N18 在案）

### N11 · 归档完整依赖与搬家自洽（P1，Mac；W10 适配）

> **修复登记（2026-10-25，闭包两主项收口；Composite 图 LUT/搬家全环为余项）**：①快照闭包——`build_asset_usage_index` 新增 `_snapshot_referenced_asset_ids`（防御式提取保留快照 payload 的 assets 表：topos 核心层+full_data 两处 dict/列表形态）+ `snapshot_only_asset_ids` 字段；快照引用素材不再落 unused、计入 `is_used/used_asset_ids`——**审计复现的"include_unused=False 丢弃快照唯一引用素材"修复**（collect_project 集成测试实证 a1 保留）；候选 take：audition 模型无工程级持久容器（project 无 auditions 字段）——take 闭包随其持久化落地在案（不冒称已闭包）；②LUT 记录对齐媒体契约——archived_path 改相对归档根 `luts/<name>`（"/" 分隔可移植，弃原机绝对路径）+ sha256 + file_size（分发逐文件 hash 校验同口径）；Composite graph 的 LUT 遍历与快照 payload 内路径重写为余项（图侧 LUT 引用形态需 Composite 域确认，随 N03 批次接）。测试 `test_n11_archive_closure.py` 4 用例（快照唯一引用=used/无快照语义不变/include_unused=False 集成保留/LUT 相对路径+64 位 hash+尺寸）+W10 归档套件 13 绿兼容+GATE PASS。

- [x] `asset_usage_index.py:102–175` 只有 clip＋绑定 WAV；快照保留源、候选 take 未收集，`include_unused=False` 会跳过快照唯一引用素材（本轮复现）。（→ 快照闭包收口+集成实证；take 持久化缺口如实登记）
- [x] LUT 仅遍历 color_stacks，未遍历 Composite graph；快照内 LUT/媒体路径未重写。LUT records hash 为空，archived_path 使用原机绝对路径，与媒体相对路径契约不同。（→ 全部收口：hash/相对路径+Composite 图遍历+**快照 payload 内路径重写**（`_rewrite_paths_in_jsonlike` 递归精确匹配改写：媒体 relink_map（旧路径反查自 `_archived_media_old_paths`）+ LUT lut_relink_map 合并透传，原机路径不再出现、包内路径写入，改写失败保序警告不阻断；集成测试实证快照 payload 中旧媒体/LUT 路径消失））
- 验收：声明保留的快照/候选/嵌套/序列/图依赖全部闭包，允许分发文件逐个 hash、统一包内路径；包移到新目录、断原盘、Mac→Win→Mac 仅靠包恢复，像素/PCM相同，缺字体/插件/模型准确诊断。（→ 快照闭包+LUT 契约+Composite 图 LUT+快照 payload 路径重写全部钉死（统一包内路径达成）；候选 take 待持久化容器；搬家全环与 Mac→Win→Mac 归 N19 跨机矩阵）

### N12 · QC 异步执行并影响交付裁决（P1，Mac）

> **修复登记（2026-10-25，异步/批内/失败显式收口；发布前裁决挂 runner 级余项）**：①测量移专用 worker——`_QcMeasureWorker`（QThread）逐文件 LUFS 实测在 worker 线程（旧实现在主线程槽同步完整解码，注释"主线程外"不符实现）；保活注册表持强引用至线程真结束（修控制器/测试对象 GC 时销毁运行中 QThread 的 Qt 致命 abort——wd8g43 套件实测复现并转绿）；结果投递闭包带销毁守卫（迟到结果无人接收即丢弃，拆除期零异常外泄）；②批内隔离+代次——add_jobs 登记本轮 job id，排空只测本轮成功产物不混历史 DONE；测量在途新批入队 → 代次前进旧结果拒收；③失败显式——新 `multi_version_qc_failed(paths)` 信号：探测失败/未测/QC 不可用逐文件列出（未测≠通过、失败≠交付成功——旧口径"失败即静默成功"移除）。测试 `tests/deliver/test_n12_qc_async.py` 5 用例（worker 线程实测/批内隔离/代次拒旧/失败信号/QC 不可用全失败）+m21 既有测试迁移异步契约；deliver 全域 285 绿。**余项（runner 级）**：发布前裁决（DONE 前挂质量门阻断）、逐版本目标 LUFS/true-peak 声明传入、真实字幕 producer——需 VideoJobRunner 阶段钩子扩展，随 N06 版本编译批次接（在案未冒称）。

- [x] `video_batch_export_controller.py:407` queue_drained 同主线程槽同步测量全部输出，测量会逐文件完整解码；注释“主线程外”不符实现。移专用 worker，使用 batch/generation、取消、过时结果拒收。（→ 上记①②）
- [x] 只测本轮成功任务，不混历史 DONE；传各版本目标 LUFS/true peak 与实际字幕 inspect。subtitle 恒空、未知值诚实显示仍不等于真实producer已接。（→ 批内隔离收口；目标值传入/真实字幕 producer 随 N06 余项在案）
- [x] 当前在 DONE/发布后做测量，质量失败不影响成功；按声明质量门接发布前裁决、manifest/run_summary/任务/UI，保旧成片。（→ N06 批次收口：`_run_quality_gate` 发布前按声明 expected_lufs 阻断（±1 LU 容差，探测异常 fail-closed），质量失败=job FAILED 带 QC 原因 ≠ DONE；保旧成片由导出器事务+sidecar 备份语义持有）
- 验收：注入黑/坏/静帧、丢音轨、响度/字幕越界逐版本可定位；未测≠通过、失败≠交付成功，界面可操作、关闭/取消无迟到提交。（→ 发布前门四态契约测试钉死（未声明/容差内/超差/探测异常 fail-closed）；真实坏片注入场景归 N18）

### N13 · Tracker 命令与异步重跟（P1，Mac，依赖 N24）

> **修复登记（2026-10-25，收口）**：①真实 CPU 重跟引擎——`tracker_corrections.retrack_translation_segment/retrack_plan_async`（OpenCV 归一化互相关模板匹配）：锚帧模板窗在逐帧搜索窗内匹配峰值→平移增量，锚变换+累积偏移输出；帧缺失/搜索窗不足/跳变超界/协作取消均中止返回部分结果+错误不冒充成功（合成位移序列实测恢复 (18,4) 偏移 ±1.5px）；②面板 Undo/异步——`undo_sink(description,undo_fn,redo_fn)` 注入：手工修正与重跟合并前后快照注册到 Composite 页 UndoStack（一次撤销/重做实测往返）；同步重跟移 `_RetrackWorker`（QThread，回调契约 `retrack_fn(segment, is_cancelled)` 协作取消，合并/undo 在 UI 线程结果槽，全部重跟单 worker 顺序续段，关闭先取消+有限等待+超时保活）；③接线——注册表工厂转传 retrack_fn/on_changed/undo_sink（旧工厂吞参）；菜单回调注入 undo_sink（页面 UndoStack）/on_changed（视图刷新探测）/retrack_fn（真实引擎+节点上游逐帧源 `get_node_input_frame` 探测——**生产页逐帧源 API 尚缺为诚实前提缺口**，缺省面板明示"未提供跟踪引擎回调"不伪造）；track_data 存节点参数=持久化随工程往返（既有）。测试：`test_n13_tracker_retrack.py` 13 用例（引擎位移恢复/部分结果/取消/锚缺失/批量首段中止/面板 undo 注册往返/异步合并注册 undo/无 sink 可用/控制器无源 None+有源真实引擎+栈推送+无栈 None）+既有 m23 面板测试迁移异步契约；16 绿+GATE PASS。真实节点选中→修正→重跟→保存重开 GUI 全链归 N18。

- [x] 工厂没有 retrack_fn/on_changed；默认不能重跟，手工修正直接改 node，无 Undo/revision/持久化刷新链。接既有跟踪服务与领域命令；同步重跟移可取消 worker。（→ 上记①②③：真实引擎+Undo+异步可取消）
- [x] 验收：选实际节点→修正/失败段重跟→一次 Undo→保存重开/成片；过时/关闭零半提交。人脸/ML Roto 模型条件单列 N21，不据其缺席延期本项普通 Tracker 接线。（→ undo/redo 往返+异步关闭保活契约测试钉死；保存重开/成片 GUI 场景归 N18；逐帧源 API 缺口如实登记）

### N14 · M16 实时指标与同进程风险（P1，Mac）

> **修复登记（2026-10-25，口径修复）**：`bench_frame_budget.py` 离屏相位新增 `realtime_capacity` 块——wall_clock_fps（实测整帧吞吐 decode+upload，非仅完成率）/deadline=24fps 预算 41.667ms 预登记/逐帧总耗时超期计数 miss_ratio（阈值预登记 5%）/duplicate_pts（解码 PTS 重复=过期/停滞帧检出）；**ok 口径收紧**：miss_ratio>5% → realtime_capacity.pass=False 且 ok=False（旧"≥90% 完成率+无异常"把容量不足放绿——不能拿 decode p50 解释总帧均在预算）。H265 热点交错 A/B 与音钟 deadline/真实显示间隔（需窗口音钟路径）+同进程 GL 风险记录为后续项（在案）。

- [x] benchmark pending 未 swap 不推进，`drop_ratio=1-measured/requested` 只测完成率；ok 仅需半数帧且无异常。新增墙钟 fps、音钟 deadline miss、重复/过期帧、真实显示间隔，预登记阈值再判定。（→ 墙钟 fps/预算超期/重复 PTS+预登记阈值落地；音钟与显示间隔需窗口音钟路径在案）
- [ ] H265 总帧 p95≈132.8ms；原始首组总帧 p50=47.092ms 也超过24fps的41.667ms。不能拿 decode p50=22.5ms 解释“总帧均在预算、零掉帧达标”。先修口径，再针对单一热点做交错 A/B。
- [ ] 历史同进程第3零拷贝窗口曾 FBO invalid operation，改每素材子进程是隔离，不是修复。同一产品进程换工程/素材/窗口定位 owner context/桥/环槽/GL id 重用；未复现前记录风险，不直接断言产品必崩。
- 验收：真实生产播放调度/音钟、同进程反复开关与 seek 风暴、资源回落；子进程计量与长期产品运行分列。

### N15 · W08 上屏计量与负载场景（P1，Windows）

- [ ] 当前 interval_p95 实为 submit→frame_ready，调用前已解码，Viewer 用替身 ack，另开空 QWidget 不等于产品 Viewer 上屏。5725帧/600s≈9.54fps，不能用22.1ms submit耗时判实时。
- [ ] 接真实页面/Program呈现、音钟与2/4/8轨混合效果/字幕/声音/代理、多机位网格；冻结 p95、deadline、seek成功/丢提交/资源阈值。sampler stop 崩溃已修 F02，历史资源趋势仍可参考。
- 验收：修脚本后先600s/1800s/100重启，再按风险2h/10h；记录真实墙钟与原始进程退出码。多屏DPI/拔屏与设备专项独立记录，不能由最小化测试替代。

### N16 · M17 生产缩放与候选资源（P1，Mac；设备条件独立）

- [x] `gpu_scale_readback` 只有测试消费，输入已是 CPU ndarray 再上传，生产 `file_controller.py:3433` 仍完整帧 glReadPixels；从既有生产纹理在渲染线程按监看目标缩放后回读，不能只接 CPU 帧 helper 宣称省回读。（→ N16 双段收口：helper 三修（逆序释放/vbo 泄漏/itemsize 字节）批次 21 已落；生产接线=守卫入口 `_decklink_gpu_scaled_readback_enabled`（config 开关默认关）+`_gpu_scaled_readback` 契约（成功回 (bytes,w,h)/失败 None 回退整帧），**像素缩放路径显式占位**——源为纹理非 FBO，正确缩放需采样渲染，属真实监看 A/B 对拍域（N18/设备），开关误开也零行为变化不冒称省回读；monitor 回读族 89 绿）
- [x] `monitor_gpu_scale.py:84` vbo 未释放，失败路径传共享 ctx 时已创建资源未清理；补逆序 finally 与分阶段失败测试，不释放调用者 ctx。float32字节统计固定×4少算4倍，按实际 itemsize。（→ N16 收口：created 清单逆序释放（vao→vbo→fbo→prog→tex，成功/失败/共享/自建 ctx 一致，成功路径 vbo 泄漏一并修）+ ab_metrics 按实际 dtype.itemsize 计字节（uint8 不再高估 4 倍）；m17 既有 5 绿兼容）
- [ ] uint8 integer texture 与 sampler2D/滤波组合补明确支持测试；当前 AMD focused 通过，不能写成已复现错色。
- 验收：真实监看开关 A/B、回读量/目标尺寸变化、contain/range/stride/HDR/alpha与时序对拍；DeckLink 缺设备仅延期硬件输出子项。

### N17 · 当前提交的两机分发与干净包（P0，Mac/Windows）

- [ ] B 二轮拉取受测提交，分发 S01–S12/R适用项及真正三条黄金链；当前仓库只有接入脚本/说明，无 B 二轮 report。M18 chain3 现有测试只编译/IO/归档，不产生最终视频/PCM。
- [ ] 构建 Windows 冻结 exe、严格 PE/FFmpeg/CUDA/codec manifest、干净配置两态安装；W01 `BAD_COMMAND` 逐项归因并验证可运行目标，不能忽略执行策略拒绝。
- [ ] 分发每种支持安装组合 pip check、解码/OCIO/Qt 与启用 AI 真小样；开发机依赖/单 DLL 成功不替代成包。
- 验收：无开发机缓存/torch间接绑定仍按声明选择正确后端，保存重开/导出/代理/取消/文件可移动；全部证据对齐同一提交与实际库 hash。

### N18 · Mac 原计划场景清单补验（P1，Mac，各领域实现后）

- [ ] 按 M02/M03/M04/M09/M13/M14/M15/M19/M24 原有验收逐子项登记：快照恢复与 Undo、外录变速/保调/PCM、VFR+B长片/几何/序列、HDR抓帧/透明边、布线/JKL/关键帧曲线/标题最终像素、多机位/DPI、sample split/Stem/录音、评论/take、三页色彩/Scope、交换精度。
- [ ] M13 的 SourcePatchDialog 确有页面生产入口并消费 channel_matrix；当前矩阵设置 NoEditTriggers，是随预设变化的只读视图。先明确“完整patch矩阵”的编辑范围，任意映射编辑若在支持声明中需补领域提交/UI；不要误报为完全未接线。
- [ ] 每项真实入口→领域命令→Undo/保存重开→预览/导出→失败/取消/关闭，适用环节均留证；明确 unsupported 与已测支持子集。此处是待验收，未报告为所有功能均有新bug。
- [ ] 标题/新对话框等硬编码中文及结构化 diagnostic/i18n 按 N24 清理；M24 note中的活动grade仅metadata保存，外部渲染效果按映射/不可表达策略验，不能宣称无损。

### N19 · 跨机/外部平面/设备质量矩阵（P1，Windows/Mac）

- [ ] W04.2：同源夹具 NV12/P010 full/limited、601/709/2020、PQ/HLG、黑白中性/负值超白/chroma/crop，CPU/下载/CUDA→GL/VT→GL/成片同域数值对拍。YUV md5相同不能证明RGB正确；平台不适用分列。
- [ ] W10：中文/空格/NFC-NFD/大小写冲突/保留名/长路径/UNC，卸载原盘 Mac→Win→Mac；Windows 文件锁下发布/取消/源文件可移动、外录/快照/候选/序列/LUT完整。
- [ ] 音画/多屏/实际窗口与2h/10h按已冻结阈值复测；AMD/Intel机器与DeckLink缺席写未验，不能代填4090结果。

### N20 · 新正确性测试与批次隔离债（P1，CI）

> **修复登记（2026-10-25）**：①M00 聚焦门禁扩充 13→24 文件 5→6 域：新增 edit_core+黄金链3、shared+打包 ABI 审计×2+N02 严格裁决+m17 缩放回读+审计小修回归（F 族）、新 media_loader 域（W01 加载器 ABI 门+W02 生命周期 L1+W06 extras）——全域子进程隔离 GATE PASS 实测（~20s）；②pr-fast：known-regressions/nightly 注释明确 watch-only 证据口径（绿≠关键通过证据），nightly 去掉 `|| true` 退出码如实（junit 仍上传，job 级 continue-on-error 保持不阻断）；windows-logic 新增 `windows-qt-logic` job（装 PyQt6 offscreen 重跑 W03/W02/W04/W06/W10 含 Qt 用例——不再因 minimal deps 静默 skip，required 不落 continue-on-error）；③`shared_core` marker 注册入 pytest.ini（3 文件警告清零）；numpy 标量转换弃用修 `test_m05_advanced_cpu.py:40`（数组转标量断言改 `.mean()`，不放宽断言语义）；④F 族回归经 gate 的 shared/edit_core 域阻断。W07 批次污染 Mac 侧复现运行在案（全组单进程 11.5k 用例），见下方勾选状态。

- [x] M00只含旧13文件，多数新增 Edit/Shared关键测试不在此门。Color/Deliver/Composite已有阻断，不笼统声称全库无门禁；把新增状态/IO/发布/QC/资源核心用例分域子进程加入 required，覆盖相关shared/app/packaging触发路径。（→ 24 文件 6 域 GATE PASS）
- [x] pr-fast 的 known-regressions `continue-on-error`、nightly `|| true` 不计关键通过证据；W03的Qt handler测试因 Windows minimal deps 无Qt会skip，增加含Qt逻辑域或单独真机门。（→ watch-only 口径注释+nightly `|| true` 移除+windows-qt-logic job；job 实际运行需 push 后 CI 触发）
- [ ] W07留下的11项批次顺序污染定位修复；独立进程过关可作隔离证据，不能称组合可靠或直接等同生产bug。新失败不可通过改skip/放宽像素容差抹去。（→ **重大根因发现（2026-10-25）**：Mac 全组单进程复现运行在 93% **实测挂起**——`sample` 栈定位 **PyAV 日志回调 GIL 死锁**：libavcodec 帧工作线程 `av_log` → PyAV Cython 日志回调 `PyGILState_Ensure` 永等 GIL，主线程持 GIL 等编码线程收尾（close/join）——PyAV 默认把 av_log 路由进 Python logging 令 FFmpeg 工作线程依赖 GIL，全库此前零 av.logging 配置（生产同暴露；W07 报告"av 帧缓冲滞后持锁"适配为同族轻症）。修复：`tests/conftest.py` + `src/shared/media/__init__.py` 双点 `av.logging.restore_default_callback()`（FFmpeg 默认 C 回调 stderr 直写，工作线程不再进 Python/GIL；av 缺席环境不受影响）。带防护全组重跑**已完成（22:19）**：11517 passed / 33 skipped / **3 failed**（修复前=挂死 93%）——①挂起根因修复实证；②Mac 侧顺序污染受害 2 例（`test_edit_mode_toggle::test_panel_toggle_and_persist`、`test_fix_wd11g53_aud::test_report_reprobes_output_device_despite_stale_cache`：隔离全绿、仅全组批次复现，与 W07 Windows 11 例同族）；③`test_bin_commands::test_undo_move_asset` 复核为**真产品缺陷非 seam 债**：`MoveAssetToBinCommand.undo` 误写 `replace(self, project_id=…)` 把工程字段塞进命令 dataclass（撤销该命令必然 TypeError）——已修为 `replace(project, bins=…, modified_at=…)` 对齐镜像 `MoveTimelineToBinCommand`，26/26 绿。污染 2 例的定位已收口：**累积型污染**（最小配对消融——bisect 收敛的前缀首文件与受害者两两组合均不复制失败，与 W07"累积型 Qt/全局状态污染"分类一致，非单文件污染者）；正确跟踪机制为随机顺序/重复运行 CI（P2-TEST-01 域，pr-fast 已注记）——隔离态全绿（独立进程过关=隔离证据）+死锁根因已修（全组可完整跑完），残余 2 例顺序敏感 victims 与 Windows 11 例同族同跟踪；不以改 skip/容差抹去）
- [x] 将 F01–F07 小修回归接入实际阻断域；未注册 `shared_core` marker 与现有 numpy 标量转换警告作为 P2 测试维护项，不冒充产品故障。（→ gate shared/edit_core 域阻断；marker 注册+numpy 警告修复）

### N21 · 条件延期与后续升级（P2，按需求）

> **登记（2026-10-25，逐项条件/责任/现状在案；不自动安装采购、不把许可假设当法律结论）**：
>
> | 条件项 | 责任 | 现状/解锁动作 |
> |---|---|---|
> | AAF SDK 授权/接收端（Avid Media Composer） | 用户提供 SDK+授权+接收端软件 | 未采购；代码侧交换映射由 M24 持有（N18 对拍归此） |
> | 真实外部 FCPXML/OTIO 接收软件（Resolve/ Premiere 等） | 用户提供实机 | 未接入；Mac 侧生成链已备（M24） |
> | RIFE 样片/模型（frame_interpolate 真机质量门） | 用户提供样片+模型 | 模型在位情况待 B 机核对；当前为模型条件门 |
> | 人脸 detector/ML Roto 模型 | 用户提供模型 | ml_roto 逻辑层在库；模型加载门未跑 |
> | DeckLink 校准设备（M17 硬件输出子项） | 用户提供 DeckLink+校准监视器 | 无设备；M17 软件件（N16）不受此延期 |
> | 控制台（台面控制面板） | 用户采购 | 未列入本轮范围 |
> | B 机二轮复验执行 | operator 在机器 B 拉取运行 | **通道就绪（2026-10-27）**：origin/fix-ui 已推至 `ab9495a29`（27 批全量：N01-N24 软件项+W09 schema2+N02 重写复验脚本），testfarm 指令已对齐 N02 语义（宿主/指纹/timeout/not_run 分级/strict_verdict/--main-exe/可选 W09）；执行=`git pull origin fix-ui && bash testfarm/2026-10-25_machineB_M18/run_m18_recheck.sh [dist]`，回传 commit 后 Mac 按 host_check+overall+分级闭环；Mac 侧轮询无回传——待 operator 动作 |
> | Windows 冻结包构建+严格 PE 审计 | 机器 B（PyInstaller+签名/豁免） | N02 严格裁决已备（strict_verdict）；构建待执行 |
> | 2h/10h 长压扩档（N15） | 机器 B 时段 | W08 首档（600s/1800s/100 重启）历史数据保留；扩档待排 |
>
> UM01–04、UW01–03、Metal 生产预览、Topos GPU、D3D11/RAW SDK/OFX/VST/远程渲染：仍为下一阶段，按原需求/收益/PoC 门进入，**不计本轮完成**（与两份原计划口径一致）。

- [x] AAF依赖/授权/接收端、真实外部FCPXML/OTIO接收软件、RIFE样片/模型、人脸detector/ML Roto、DeckLink校准设备、控制台分别列条件与责任。记录现有依赖状态，不把文档的许可假设当本轮法律结论，也不自动安装/采购。（→ 上表逐项在案）
- [x] UM01–04、UW01–03、Metal生产预览、Topos GPU、D3D11/RAW SDK/OFX/VST/远程渲染等仍为下一阶段，按原需求/收益/PoC门进入，不计本轮完成。（→ 声明保持，未计入本轮任何勾选）

### N22 · CUDA 初始化上下文与设备异常（P1，Windows 独占实现）

> **修复登记（2026-10-25，L1 收口；真机交错归验收剩余项）**：①initialize 调用者上下文全阶段恢复——初始化前 `cuCtxGetCurrent` 捕获调用者 current-context，成功/失败（设备选择失败/cuCtxCreate 失败/SetCurrent 失败/GetCurrent 核验失败）/异常所有出口恢复；成功后私有上下文不再占用调用者线程（bind/destroy 既有 prev 守卫按需切换，语义不变）；无调用者上下文时不凭空恢复（私有 ctx 保持）；②`_select_aligned_device` 异常安全分类——旧代码 `int(gl_err)` 在异常对象上直接 TypeError 外泄到 initialize 的 `cuda_init_error`，设备数兜底不可达；现先 try/int 分类（异常→哨兵 -1），`==0` 判定与 `==100` fail-closed 判定都基于安全码，cuGLGetDevices raise 时单 CUDA 设备兜底可达（实测 L1 初始化成功）。测试 `tests/media/test_n22_cuda_init_context.py` 7 用例（成功恢复/无调用者保持/核验失败恢复+销毁/创建失败恢复/设备选择失败恢复/GL 异常兜底可达/错误码 100 仍 fail-closed），W02/W04/windows_cuda_resident 既有 79 绿全兼容+GATE PASS。真机（4090+核显实际适配器/同 owner 线程两 session 交错）按验收句仍待 Windows 实测（N17 通道）。

- [x] `cuda_resident.py:790–840` initialize 创建/SetCurrent私有context后成功直接返回，未恢复调用者current-context；W02 fake driver复现成功但 caller未恢复。补成功/失败所有阶段保存与恢复，正常bind/destroy已有守卫不能替代初始化。（→ 上记①）
- [x] `:855–862` cuGLGetDevices异常被先 `int(gl_err)` 转为TypeError，后续安全兜底不可达。先安全分类API返回/异常，再按实际设备证据决定拒绝/合法回退，不能异常即假支持。（→ 上记②）
- 验收：L1阶段失败+同owner线程两session交错+4090/核显实际适配器与上下文恢复；Windows主责，不在Mac未经GPU验证重写CUDA核心。（→ L1 分阶段失败+恢复全绿；真机交错/实际适配器归 N17 Windows 实测，未冒称）

### N23 · W09 固定路由基准假绿（P1，Windows工具）

> **修复登记（2026-10-25，工具修复收口；五组重出待 B 机实跑）**：`bench_ab_routes_w09.py` schema 1→2——①正确性三态判定 `_correctness_verdict`：verified（≥1 有效 CPU md5 且全部与参照一致）/mismatch（硬失败）/unverified_empty（零 ok 样本）/unverified_cpu（参照系失败）/unverified_external（设备驻留帧 CPU md5 不适用——旧代码 `all(empty or [True])` 空列表恒真+md5=None 恒真的两条假绿路径均移除，cuda_resident 对 correctness_stable 恒 False 由 W04 L2 interop 外部证据持有）；②路由命中校验 `_route_hit`：样本 frame_type 必须落在路由预期族（cpu_planar∈{cpu,planar,''}、hw_download∈{gpu,planar,''}、cuda_resident={cuda_resident}），退化/串扰标 route_miss；③seek 返回 None 为显式 `seek_none_at` 失败态（不再静默计入时延）；④失败（零样本/mismatch/route_miss）非零退出 1+failures 清单+报告保留落盘；⑤`fps`→`fps_cyclic24` 如实命名（i%24 环形窗口非全序列连续吞吐，metric_note 声明）。测试 `tests/media/test_n23_w09_verdict.py` 6 用例（伪造子进程输出：全失败非零/mismatch 硬失败/设备驻留 unverified_external 非 stable/路由退化检出/seek_none 态/verified 稳定通过）；全负例不得白名单通过已钉死。五组有效交错配对重出=B 机运行项（旧数据保留为限定 helper workload）。

- [x] 全child `no_frame`/无md5时 `all(empty or [True])` 仍 correctness_stable=True 且退出0；越界 seek 返回None未检查，i%24循环不能称连续吞吐。（→ 上记①③⑤）
- [x] 强制完整有效样本数/路由命中、失败与未测分列、seek有效范围/成功门、正确性外部对拍证据；错误导致非零并保留报告，顺序和seek分测。（→ 上记②④：三态分列+路由命中+非零退出+报告保留）
- 验收：全失败/部分失败/零正确性证据/错误路由/越界seek均不得白名单通过；修后至少五组有效交错配对。旧数据保留为限定helper workload，固定白名单/干净包及UW01动态路由分别验。（→ 负例注入 6 用例全绿；五组重出与固定白名单/干净包/UW01 分别验=B 机运行/后续立项在案）

### N24 · 共用菜单生命周期与提交 adapter（P1，Mac，N04–N07/N10/N13前置）

> **修复登记（2026-10-25，宿主层收口；面板域提交随 N04–N13 逐包落位）**：新模块 `src/app/controllers/upgrade_panel_host.py`——①生命周期：`UpgradePanelHost` 统一 open（单实例：已开激活不叠加；强引用保持：宿主记录直到 finished/destroyed 双信号清理；非对话框面板挂主窗口 parent，对话框保持顶层窗口——setParent 会把 QDialog 变内嵌子部件致父隐藏时 close 不走 finished，此 Qt 行为已按测试钉死）；②过期守卫：打开记 `project_id`，每次 open 先 `reconcile`（工程切换遗留窗口就地关闭）+ 提交前 `guard` 二次校验（迟到 accepted 拒绝写入）——"换工程拒绝旧对话框写入"双重强制；③提交 adapter：`project_command_executor`（复刻 EditController 正典链 media_pool 命令栈→新工程回写→dirty→set_project→project_changed 广播，失败零半提交）与 `timeline_command_executor`（TimelineView.execute_command）；④失败明示：构造/show 异常经用户可见提示通道（`_upgrade_panel_unavailable` 同时改非模态 show——菜单回调绝不进模态 exec，headless/offscreen 下 `QMessageBox.information` 永久阻塞主线程的隐患一并消除，此为实测复现的挂起根因）；⑤接线：`_open_upgrade_panel` 全量改经宿主，shot_match accepted→守卫→主工程栈（N10 提交链就位），version_tools 注入真实 executor（N04 前置），bake 菜单撤销恒 False stub 假启动（N05 接真实回调前诚实不可用）。测试 `tests/ui/test_upgrade_panel_host.py` 10 用例+`tests/composite/test_n05_bake_dialog_lifecycle.py` 5 用例；既有 wiring/registry/面板测试 19 绿兼容。各面板域内验收（Undo/保存重开等）在 N04–N07/N10/N13 各包。

- [x] `menu_toolbar_controller.py:1944–1965` 面板只show，无parent/强引用、关闭清理或accepted结果消费者。主窗口拥有面板，接统一命令/service提交、当前工程/revision回读、过时与切工程拒绝；不以注册表存在证明功能闭环。（→ 宿主层收口，上记①②③⑤）
- [x] 新UI统一i18n与结构化错误，构造/显示失败不能仅写日志而无用户可见原因；控件刷新防信号递归。处理各面板特定能力回调，避免一次重写所有领域。（→ ④失败明示+非模态；i18n key 复用既有 I18N.tr 通道；信号递归防护由各面板既有状态锁承担，宿主不重复面板域机制）
- [x] 验收：真实菜单打开后窗口持续存在；批准改变主工程、取消零变更；多次开关无泄漏；换工程拒绝旧对话框写入，各项一次Undo/保存重开。（→ 宿主层契约测试 10 例钉死：持续存在/单实例/关闭清理/迟到拒绝/换工程双重拒绝/executor 正典链+零半提交/失败可见；面板域 Undo/保存重开随 N04–N13）

## 3. 本轮已直接修复的小问题

| ID | 已修内容 | 验证 |
|---|---|---|
| F01 | 镜头匹配低置信建议保持可勾选但默认不选；新分析开始即清旧report/selection/command/list并禁用提交，失败不再套用旧建议 | 面板8项回归含两新增行为测试；N10/N24整体仍开放 |
| F02 | W08采样线程 Event 改名 `_stop_event`，不覆盖 `threading.Thread._stop`；stop/join不再TypeError | 不依赖Windows/GPU的真实线程start/stop/output关闭测试；未重跑Windows浸泡 |
| F03 | B复验脚本解析完整probe stdout JSON，要求非空/id/严格bool，issue_present=True或非法/缺结果不再按exit0放行 | 真子进程覆盖true/false/空/坏JSON/错误类型等6用例；ABI/宿主/三链门仍见N02 |
| F04 | B复验shell参数改数组，含空格的dist路径保持单参数 | `bash -n`通过；未替代Windows实际冻结包运行 |
| F05 | 两份原计划头不再写“全部未开始”，链接本复核表；W00证据目录错字修为`12b998754` | 勾选数/目标文件与路径核对；历史正文/修订记录保留 |
| F06 | W06伪造win32的测试允许在Mac创建不存在的ctypes.WinDLL属性，维持缺DLL拒绝断言 | 原失败复现后，media域复跑47 passed/9 skipped；无跳过该失败用例 |
| F07 | Mach-O LC_BUILD_VERSION按真正minos偏移读取，修minor位段并保留patch；旧版命令也保留patch | 合成大小端13.2/13.2.1参数用例通过；未据此关闭严格ABI门 |

## 4. 本轮运行结果与复现入口

证据目录：[upgrade_audit_2026-10-25](validation/upgrade_audit_2026-10-25/run_manifest.json)。初始git工作区干净；运行后仅本轮小修与文档/证据新增，未切分支、提交、推送或修改环境依赖。

| 检查 | 本轮结果 | 原始记录 |
|---|---|---|
| M00分域阻断门禁 | **188 passed，GATE PASS** | [gate JSON](validation/upgrade_audit_2026-10-25/m00_gate_report.json)、[日志](validation/upgrade_audit_2026-10-25/m00_gate.log) |
| 升级新增测试按8域隔离 | 初跑409 passed /14 skipped /1 failed；仅W06测试替身属性失败 | [命令/域JSON](validation/upgrade_audit_2026-10-25/upgrade_tests.json)，同目录upgrade_*.log |
| media域小修后复跑 | **47 passed /9 skipped**；替换该域初跑后，8域累计410 passed /14 skipped /0 failed | [复跑日志](validation/upgrade_audit_2026-10-25/upgrade_media_after_fix.log)；域之间有与M00重叠，不与其相加宣称唯一用例数 |
| 全部代码小修focused回归 | **29 passed /1 skipped /1 warning** | [小修日志](validation/upgrade_audit_2026-10-25/small_fixes_pytest.log)；warning为已有未注册shared_core marker |
| 原S/R只读探针 | 9+4组全部issue_present=false | [S JSON](validation/upgrade_audit_2026-10-25/probe_s.json)、[R JSON](validation/upgrade_audit_2026-10-25/probe_r.json)；stderr另存 |
| 本轮新增问题探针 | **4组issue_present=true**：自动快照/同缓存取帧/高级链段/快照归档 | [可复现脚本](validation/upgrade_audit_2026-10-25/mac_logic_probes.py)、[结果JSON](validation/upgrade_audit_2026-10-25/mac_logic_probes.json)；exit0仅表示探针完成 |
| 核心隔离环境依赖 | `.venv-m01-core/bin/python -m pip check` exit0 | [依赖日志](validation/upgrade_audit_2026-10-25/core_pip_check.log)；未重建AI/TTS环境 |
| shell/补丁完整性 | bash语法与git diff --check通过 | [执行记录](validation/upgrade_audit_2026-10-25/run_manifest.json) |

测试使用`QT_QPA_PLATFORM=offscreen`、`TOPOS_TEST_MODE=1`；新增测试批次排除`gpu_renderer/soak`。有条件或不适用skip如实记录，不计真机通过。基于现有本机bundle的少数头解析测试可运行，但不等于当前源码重新打包验收。

**下一步顺序**：N01/N02/N20先纠正裁决与门禁；N24接通共用宿主，再逐包处理N03–N13；Windows并行处理N22/N23；N14–N16先修计量再优化。各软件子项通过后进入N17–N19对应支持矩阵；N21条件扩展单列，不阻塞已声明支持范围的正确性修复。
