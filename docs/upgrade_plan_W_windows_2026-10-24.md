# 升级计划 W · Windows 主责（整体快速复核后的原生链路收尾与下一阶段升级）

> 日期：2026-10-24；审查基线：`9e05556c3`。本轮执行机为Mac，Windows结论来自当前源码与已存真机报告；本次未重跑Windows GPU/GUI。
> 状态更新：2026-10-25 复核。正文主包勾选为 11/11，但冻结包、合并后共享复验、色彩矩阵与跨机脱机往返等仍未验收；不能按整包全部完成理解。最新逐包结论和新增缺口见[复核与收尾计划](upgrade_audit_followup_2026-10-25.md)。下文初始问题与历次修订保留历史口径；CUDA驻留仍 opt-in 默认关闭。
> 配套：[Mac计划M](upgrade_plan_M_mac_2026-10-24.md)，由Mac主责共享修复、Windows复验；避免重复修改共享模型/renderer。

## 0. 当前结论与协作规则

上一轮Windows PoC、驻留接线及短压确有交付；条件未触发的D3D11/Topos GPU解码决策也予以保留。但旧DLL红基线、现有源故障恢复、context/部分资源回滚、设备门和seek快照尚未闭环，不能据短压或“零新增回归”默认启用加速。

历史机器B为i9-13900K/RTX4090，旧报告Python3.13.12/PyAV18.1.0/驱动621.01；执行时重新采集，不能把这些数值写成当前机器事实。机器C/AMD或其他Intel设备的结果各自采集，不平均，不代填。

初次Mac复核已跑12个focused文件：142 passed，9组逻辑复现观察到缺口；v2补4组只读遗漏探针，未重跑142项，也未执行Windows/GPU，不能当成Windows已验证。证据：[执行指纹](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/run_manifest.json>)、[问题总索引](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/issue_inventory.json>)、[原接续与已知限制](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/feature_F/F0.1/0b57c322d/manifest_2026-10-24.md>)。

- 本文的Mac绝对跳转不是Windows唯一证据入口：[可移植源码/证据索引](validation/upgrade_2026-10-24/portable_source_index.json)保存仓库相对路径、行号和审查commit。Windows先以`git rev-parse --show-toplevel`取得本机根，再解析索引；含空格路径须引用。
- Windows独占`windows_hw/`、平台探针/原生DLL与Windows打包；共享schema、帧/租约、Color/音频契约及两端renderer由Mac集成者统筹，按文件白名单串行合入，不能两端同时改同一共享文件。
- 先修安全释放、帧/声音/颜色与恢复，再测收益；P0为开工/发布门禁，P1为正确性及核心收尾，P2为推广/效率。缺硬件/依赖只关闭相应能力，不伪造通过。
- 每包遵循`.clinerules`：GPU只在既有渲染线程/队列，取消/异常/关闭显式清理，命令/Undo/版本化迁移一致，不静默丢效果或改音源。

## 1. Windows发现的全部问题与责任包

| ID | 证据层级 | 触发与问题 | 当前入口 | 责任包 |
|---|---|---|---|---|
| W-I01 | 既有 Windows 真机报告 | 旧 topos_codec.dll 缺 tc_mux_add_audio；首跑 1023 error、208 fail，与旧红基线一致不能当发布通过。打包库查找只验存在，缺平台/架构/ABI 校验。 | [test_farm_first_run_windows_2026-10.md](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/test_farm_first_run_windows_2026-10.md:38>) | W01 |
| W-I02 | 静态调用链确认 | CUDA 导入失败仅 render_error→记丢帧；门闩只管新源，已有 resident 源未见下载/CPU 重建链。 | [timeline_preview_renderer.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_preview_renderer.py:11387>) | W03 |
| W-I03 | 控制流确认／无 GPU 方法替身检查 | 私有 context 创建后 destroy 未调用 cuCtxDestroy；第二平面注册失败未回滚；32 个空闲异 key slot 仍耗尽。真机资源趋势待测。 | [cuda_resident.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/media/windows_hw/cuda_resident.py:510>) | W02 |
| W-I04 | 静态能力门缺口 | 固定 CUDA device 0，未闭合到 GL/解码设备；仅凭 itemsize 推格式，缺 plane/shape/pitch/chroma/device 的完整验证。 | [cuda_resident.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/media/windows_hw/cuda_resident.py:286>) | W04 |
| W-I05 | 既有真机缺陷＋静态消费者 | 快照底部陈旧纹素已登记，快照仍入 seek 缓存；L3 同帧断言容忍近 50% 字节不同，不能证明缓存正确。 | [test_cuda_resident_playback_l3.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_cuda_resident_playback_l3.py:298>) | W05 |
| W-I06 | 静态分发缺口 | cuda-bindings 依赖来自开发环境，无 Windows CUDA extra；冻结包/运行能力检查未收口，Windows logic CI 未纳入新 L1。 | [pyproject.toml](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/pyproject.toml:52>) | W06 |
| W-I07 | 待验收 | 驻留默认关闭；91s 顺序/60s seek/8 次重开不能替代窗口浸泡、长压、多轨、多屏与设备事件；AMD/Intel/A-C 矩阵仍缺。 | [c31_stress_cuda_resident_2026-10.md](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/c31_stress_cuda_resident_2026-10.md:15>) | W08 |

补充定位：资源创建/销毁 `cuda_resident.py:510/540`，slot回滚/池上限 `:624`；格式门 `:342/:391`；错误发射 `edit_gpu_preview_renderer.py:2432`；快照捕获同文件`:5096`，seek缓存消费 `timeline_preview_renderer.py:11410`。缺陷修复前先补相应focused回归，不用弱化图像断言收口。

## 2. 共享问题在Windows上的必验清单

以下由Mac主责实施；Windows必须接收相应提交并跑真实生产消费者。完整修复步骤见M计划同名责任包，本表足以用于Windows分发验收。

| ID/责任 | Windows必须验证的场景 |
|---|---|
| S01/M02 | 有metadata/评级/调色/SmartBin/嵌套的快照→恢复→正式保存重开，表和内容保留，真实dict时间线比较可检出差异。 |
| S02/M11 | 口头语plan进入真实compiler/apply/Undo；真实track/clip/词区间，链接A/V/字幕/marker一致。 |
| S03/M10 | 母版→9:16经过typed job/runner确为1080×1920；离线/冲突零入队，恢复检查源revision；PCM进入manifest/QC。 |
| S04/M04 | 真实VFR+B帧+长音频，首/中/尾和随机seek、非零入点、代理/原片按显示PTS对拍。 |
| S05/S06/M03 | 外录700Hz与机内300Hz绑定：±TC、24/25/29.97DF、1×/2×/反放/曲线/保调，预览/成片/Stem同音源同起点。 |
| S07/M12 | 源卡hash中途读失败返回逐项报告；取消/路径边界、同名异卡、目标断开与Windows文件锁。 |
| S08/M05 | pre→main→trim→post非交换向量，CPU/GPU预览/导出对拍；不是只测helper顺序。 |
| S09/M07 | 真实嵌套插件清单/缺插件或版本不兼容/探测失败；激活版本、锁定、Undo/重开不丢参数。 |
| S10/M08 | Merge端口交换必须stale；失效真正阻止旧bake进入预览/成片，换盘后依赖身份正确。 |
| S11/M06 | Qualifier动画0/48/96、非零入点/反放/2×在Color/Timeline/成片同帧同遮罩，能力不支持则预检明确拒绝。 |
| S12/M09 | 真实Gallery抓HDR float→保存重开→设参考消费EXR，4.5/-0.25保留，difference/显示变换正确。 |
| R01/R02 · M19/M05 | 同OCIO配置、三页工作/显示与Scope采样域；完整组曲线/插件/遮罩/LUT经Composite和成片，对不支持效果明确bake/拒绝。 |
| R03/R13 · M20/W10 | 间接引用外录/候选/快照与LUT等依赖闭包，规范目录签名和目标文件名；两机脱机归档往返。 |
| R04/R05/R09 · M21/M08 | 失败/取消/锁冲突保旧成片及旧bake；真实黑/静/坏帧/音轨/响度/字幕producer→裁决→manifest/run_summary/UI同版本；未测≠0。 |
| R06 · M24 | 已声明交换子集的采样偏移/实际时间基/活动grade映射；不支持字段准确报告，不能漏字段后宣称无损。 |
| R07/R08 · M22/M23 | 已启用模式通过真实前台批准/Undo/重开/成片；模型缺失明确不可用，不以透传图像冒充跟踪成功。 |
| R10/R11 · M15/M14/M09/M10 | 评论重复源优先原lineage并报告歧义；take fit时长、NTSC sample split连续性与RGBA透明边外部回导；取消入库不能显示全通过。 |
| O01/O02 | 45/53勾选分清模型/API与用户验收；F5.2/F6.3/F10仍条件未完成，已知红基线不能作为发布绿灯。 |

## 3. 可执行工作包

### W00 · 接收共享基线与明确支持边界

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W00/12b998754/manifest.md（原范围/优先级：优先级：P0；前置：—。）
- 对应：O01/O02。
- 实施：记录本机OS/CPU/GPU与显示适配器、驱动、Python/Qt/PyAV/FFmpeg/CUDA绑定、原生DLL真实路径/hash/符号、HEAD/dirty。接收Mac共享修复后逐个复验；标默认下载planar、opt-in驻留、CPU、未支持/未验证，不能把A/B/F的勾选数当上线标准。
- 验收/交付：提交Windows能力/环境manifest、现有失败ID和输入工程hash；本机环境与旧B矩阵各列，不自动复制Mac venv或改驱动。冻结指标、允许退化和取消/释放契约。

### W01 · 重建正确的Windows codec并清理红基线

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W01/9a1be7513/report.md（原范围/优先级：优先级：P1；前置：W00。）
- 对应：W-I01/O02。
- 实施：从与Python绑定一致的源码/工具链重建DLL，强校验架构、版本、SHA256、实际加载位置和tc_mux_add_audio等必需ABI；打包查找按目标平台过滤而非只看文件存在。先跑DLL load/符号与视频+音频小样，再重跑受影响media/export用例，逐项清理既有失败。
- 验收/交付：Topos真实视频+音频导入/导出/代理/取消/重开通过；缺符号启动/打包明确失败或能力关闭，不悄然选陈旧库。旧1023 error/208 fail须重新归因，不能仅凭新增0判发布通过；剩余失败有复现与阻断级别。

### W02 · CUDA生命周期、异常回滚与池预算

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W02/620168431/report.md（原范围/优先级：优先级：P1；前置：W00。）
- 对应：W-I03。
- 实施：分阶段创建私有context/纹理/注册资源，失败逆序回滚；正常销毁在owner GL线程调用实际context销毁。第二平面注册失败必须撤销第一平面与两张纹理。空闲异key slot按字节预算/LRU回收，活跃租约不回收；initialize/bind/unregister/destroy各阶段验证实际CUDA current-context并按绑定契约保留/恢复原上下文，不能只靠thread id相同推断正确。跨线程/关闭/失效路径保留可诊断状态。
- 验收/交付：逐阶段注入失败，100次开关renderer、超过32种尺寸/格式、饱和队列与seek/关闭；context/注册资源/纹理计数和显存停止后回落，release恰一次。同owner线程交错两个session或受控current-context变化，资源操作仍属预期私有context，调用者context正确恢复。无GPU L1用替身确认控制流，Windows H级再测真实资源；二者分列。

### W03 · 运行中驻留源失败后的真实恢复

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W03/18c74cf4f/report.md（原范围/优先级：优先级：P1；前置：W02。）
- 对应：W-I02。
- 实施：将导入失败关联asset/source/generation，而非只记丢帧；停用故障源的新驻留请求，待在途租约归零后重建下载planar/CPU源并限次重试。门闩需有合理故障隔离范围、诊断与显式恢复条件；避免单坏源持续黑帧或影响全部正常源。
- 验收/交付：播放中注入UV拷贝失败/会话失效，当前片段自动恢复且后续seek正确；其他轨正常，实际后端/原因可见。旧源关闭/源帧/slot只释放一次；取消或换工程无迟到提交。验证真实错误handler→重建→重新呈现，不只测门闩函数。

### W04 · 设备匹配与平面格式能力门

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W04/ab1b37569/report.md（原范围/优先级：优先级：P1；前置：W02、W03。）
- 对应：W-I04。
- 实施：显式对齐解码器/CUDA/当前GL适配器，拒绝固定device0的隐含假设；验证DLPack设备身份、plane数量、dtype/shape/pitch/chroma、可见区域和crop。Unsupported组合走正确下载/CPU路径，保留完整原因；缓存能力结果要含设备/驱动身份和失效条件。另开W04.2色彩数值子项：核bit_depth与8/10bit range系数、matrix/primaries/transfer、chroma siting、crop和alpha能力；先明确FBO的工作/显示域，再处理合法输出限幅，工作域不无理由clamp。共享shader/帧ABI由M19集成，Windows提供驻留生产路径证据。
- 验收/交付：4090+核显显示组合/强制错设备、420/422/444、8/10bit、奇数/裁切尺寸逐格；不能匹配即回退，不错读UV/越界、不冒充驻留。NV12/P010 full/limited、601/709/2020、PQ/HLG、黑白/中性/超白/负值及色度定位按同一工作域参考对拍：CPU planar/下载/CUDA→GL与Windows成片由Windows执行，VT→GL由Mac的M19产出同源夹具结果，Windows对VT行记“不适用”，不能记“通过”。YUV平面md5相同不等于最终RGB正确。带alpha格式按能力准确回退，无需强行CUDA支持。AMD/Intel另机取证，缺机器明确未验；不要求先重写D3D11。

### W05 · 修复快照读回并恢复严格缓存正确性门禁

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W05/c977d32d0/report.md（原范围/优先级：优先级：P1；前置：W02；修改共享renderer先与M05/M06协调。）
- 对应：W-I05。
- 实施：追踪_capture_frame_snapshot的FBO/viewport/scissor/跨上下文同步/generation；区分快照与present显示主链。修复前隔离异常快照，不能继续污染seek缓存；去掉同帧允许近50%字节不同的宽松断言，改为参考向量和明确位深容差。
- 验收/交付：120→40→120、背景预缓冲/seek交错、分辨率/源切换，planar与CUDA各跑；快照、缓存再次呈现和同帧参考一致，边缘/底部整帧覆盖。preserved present md5只作另一证据，不能替代快照验收。

### W06 · 可选CUDA安装组合、冻结包与能力诊断

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W06/77f820aa4/report.md（原范围/优先级：优先级：P1；硬依赖：W00。条件依赖：原生codec子项依赖W01，真实驻留验收依赖W02–W04的本机声明支持子集；extra声明、干净安装、无extra回退和冻结包准备可先做，不等待完整设备/HDR矩阵。）
- 对应：W-I06。
- 实施：声明受测Windows CUDA extra/版本范围，记录当前实测PyAV18.1和cuda-bindings13.x的能力依赖；不把所有用户强制装CUDA。核PyInstaller可选动态导入/绑定收集与运行探针；缺extra正确回退。核心/AI/可选GPU依赖组合分开锁定并在干净环境pip check。
- 验收/交付：新venv和冻结exe分别跑无extra→下载/CPU，有extra→真实驻留；不依赖开发机torch间接包。安装包记录DLL/FFmpeg/CUDA探测、实际后端、缺依赖原因；只更新声明不算交付。

### W07 · 共享正确性复验和跨层CI

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W07/9f9a969fd/report.md（原范围/优先级：优先级：P0（发布前）；硬依赖：W00、W01；接收已声明支持范围的M02–M12/M19–M24相关子提交逐项复验，不等待无关模型/算法/UI条件扩展。）
- 对应：S01–S12/O02/W-I06；R01–R12的适用支持子项。
- 实施：按§2逐项验共享代码；Windows logic CI补新L1门闩/释放/能力拒绝/失败回滚测试，核心正确性不能留在continue-on-error。编译器产物进入真实job/runner、快照进入正式IO、动画进入实际导出，避免同一dict字段自验。GPU L2/L3与逻辑CI分开。
- 验收/交付：三条黄金工程在真实Windows窗口保存重开/Undo/最终像素/PCM正确；设备差异有声明。对新增失败阻断，已知红项按W01清理。额外验输出open/flush/probe/rename失败保留旧成片、QC未知不冒充0问题、bake半套不发布、RGBA透明彩边、评论重复源歧义和sample split连续性。文件锁下取消后素材可移动/删除，工作区不被测试改写；不把非Windows替身当Windows真机通过。

### W08 · 真实交互、多轨与资源长压

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W08/fe0b9d62f/report.md（原范围/优先级：优先级：P1；前置：W02–W07适用项。）
- 对应：W-I07；F2.5/F6.6/F1.4。
- 实施：先10min播放/30min窗口交互，再按风险2h/10h；记录墙钟时长而非离屏处理帧折算。覆盖2/4/8轨混合4K、效果/字幕/声音、多机位网格与Program同跑、代理切换清理、最小化/失焦/恢复、多屏DPI/拔屏、重开/显存竞争。设备事件先可控注入，不主动破坏驱动。
- 验收/交付：记录呈现间隔p95、掉/重复/过期帧、首帧/seek、RSS/显存/队列趋势及停止回落；冻结阈值后判定。控制台/监看/硬件缺失单列未验；TDR只在适当隔离条件做有计划验证，不改系统TDR设置掩盖挂起。

### W09 · 固定策略候选对比、opt-in和发布包验收

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W09/e950d5e15/report.md（原范围/优先级：优先级：P2；硬依赖：W00；本次发布支持子集的W01–W08/W10正确性须通过。与运行时自适应路由UW01分开：本包不是动态控制器实现。）
- 实施：在固定CPU/下载/驻留候选与干净包中，按codec/位深/轨数/尺寸/机器交错A/B；保留显式opt-in与诊断/回退。只凭一次静态基准不宣称已实现运行时策略。
- 验收/交付：至少5组成对运行，绝对指标、覆盖率/失败样本和退化项齐；只有超噪声且正确稳定的子矩阵可作为固定白名单候选。源版本恢复/文件锁与本次声明支持的接收格式通过；AAF/SDK等延期项不阻塞普通交付。UW01后续实现动态切换，再以独立子门决定默认推广范围。

### W10 · 跨机归档、文件系统与脱机往返

- [x] **完成（机器 B，2026-10-25）**；证据：docs/validation/upgrade_2026-10-24/W10/0742c2f46/report.md（原范围/优先级：优先级：P1；硬依赖：W00。条件依赖：M20归档schema/闭包，M21输出事务用于对应提交；文件名/签名夹具可先做。）
- 对应：R03/R13；M20、M18；实现Windows平台适配，共享归档格式由Mac集成者持有。
- 实施：规范目录/序列manifest的路径分隔符、Unicode/大小写冲突、hash算法版本；兼容目标机非法/保留名、长路径/UNC和盘符变化。携带外录WAV、候选/快照保留源、LUT/字体/插件身份与所有序列帧；原路径不能成为唯一身份。验证Windows重命名/replace/文件锁失败恢复，源码和文档证据都用可移植索引。
- 验收/交付：Mac→Windows→Mac，中文/空格/NFC-NFD、A.mov/a.mov、CON等目标非法名、嵌套序列和外录绑定；不覆盖/错连，不把分隔符变化算成内容变化。卸载原盘后仅靠归档包重开/输出同像素和PCM；缺依赖明确报告，不静默换源/换字体。文档与证据索引在两端当前checkout均可解析。

## 4. 下一步升级路线

| 阶段/ID | 升级内容 | 进入条件与最小交付 |
|---|---|---|
| 第一阶段 | W00/W01基线与DLL；W02–W05正确性与W10跨机准备 | 运行中恢复、资源、格式门和快照先修；对应S问题随共享提交复验，不在旧错误之上比较fps |
| 第二阶段 · UW01 | 运行时自适应路由实现与切换稳定性 | W09固定候选结果到位后独立实现：按codec/轨数/尺寸/deadline选择，切换时源帧/generation/租约连续；再过“默认推广”子门。固定白名单与动态控制器分开声明，不要求UW01先完成才能验W09 |
| 第二阶段 · UW02 | AMD/Intel能力验证与受控驻留PoC | 拿到真实机器后复用五步能力门，先证明surface/adapter/import同步和收益，再接一个生产场景；不能拿NVIDIA结果替代，也不预定全面D3D11重写 |
| 第二阶段 · UW03 | 交付任务资源预算与硬编选择 | M10正确与W01/08稳定后，先测串行/小并发对前台播放和显存/编码器会话的影响；受控队列预算、背压/取消/恢复、真实10bit/字幕/音轨质量齐全后再扩大并发 |
| 跨平台升级 | UM01多机位预算、UM02缓存优先光流、UM03实测热点native下沉、UM04HDR分区/监看 | Mac按M计划实施共享能力，Windows用同源夹具做CPU/GPU/成片和打包能力验证；共享接口一个主责，平台插件各自实现 |
| 条件扩展 | D3D11合成/直送、Topos GPU解码、DeckLink GPU直送、RAW SDK/AAF、OFX/VST、多人/远程渲染 | 各自有真实需求/瓶颈、设备/SDK许可、最小PoC与收益停止条件；条件不成立继续延期，不阻塞已验核心版本。F10不是“旧计划已做完”的可用能力 |

## 5. 已有入口、执行纪律与交付模板

开工只读命令示例（在实际Windows项目根；不复制Mac绝对路径、不自动拉代码/改分支/重建环境）：

```powershell
git status --short
git rev-parse HEAD
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest tests/media/test_windows_cuda_resident.py -q --no-header -p no:cacheprovider
```

最后一条是L1/L2混合文件，必须记录pass/skip及是否存在GPU；逻辑CI按marker/具体用例分组，不能把skip算真机绿。GPU端到端现有入口为 `tests/edit/test_cuda_resident_playback_l3.py`，使用正常Windows Qt平台并独立进程运行；offscreen不能验WGL上屏。新增与真实UI验收需按修复包补齐。

已有探针/脚本：`scripts/probe_env_matrix_windows.py`、`scripts/bench_decode_matrix.py`、`scripts/stress_cuda_resident.py`；先核`--help`及当前素材目录、输出位置，再写入可复现命令。stress脚本现有`--seq-seconds/--seek-seconds/--verify-every`只覆盖自身场景，不能代替GUI多轨浸泡。

每包提交：实际base/head、文件白名单、输入媒体/工程hash、硬件/驱动/安装组合、期望与真实后端、逻辑/L2/L3/GUI/媒体/外设分级结果、原始时序与资源趋势、退出码/skip理由、回滚与未验项。目录建议 `docs/validation/upgrade_2026-10-24/<W任务ID>/<head>/<run_id>/`（未来新增）。

旧台账剩余范围一并承接：M计划§3逐项覆盖所有F域，W07/W08尤其负责F1.2文件锁、F1.4多屏/DPI/控制器、F2.5多机位、F5输入/监看、F6代理/重连/归档/交接、F7复杂图与时域、F8/F9正式IO和成片；W10承接M20归档脱机往返，W07承接M21发布/QC和M24精度交换。[原台账剩余项摘录](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/prior_plan_residuals.json>)保留详细条目。无真实设备/接收软件的专项继续未验收。

每包回滚：小提交/能力开关可回退；共享schema保留迁移和工程备份。恢复驻留默认关闭时仍须正确关闭原会话和释放在途资源，不能只关新路由；依赖/原生库失败保留已验证CPU/下载路径及明确诊断。

## 6. v2 遗漏复核与执行修正

本节记录计划缺项及其补法，**不将14条安排/验收缺项等同于14个新产品bug**。v1的142项测试和9组复现保留；v2仅新增4组只读探针，未重跑原142项，未执行GUI/GPU或Windows真机。原始证据见 [v2复核记录](validation/upgrade_2026-10-24/plan_review_v2.json)、[4组探针结果](validation/upgrade_2026-10-24/plan_omission_probe_results.json) 与 [可移植源码索引](validation/upgrade_2026-10-24/portable_source_index.json)。

| 编号 | 漏项/修正 | 明确责任 | 依据与证据边界 |
|---|---|---|---|
| R01 | 软件色彩/显示/Scope只有索引，没有实施责任 | M19；W04/W07 | `src/shared/color/color_transform_provider.py:664`；provider失败与shape恒等行为有局部复现；三页显示与Scope为静态接线/验收遗漏 |
| R02 | 完整组载荷、Composite消费和组成员编辑漏实施 | M05.1–M05.3；W07 | `src/features/edit/core/color_chain.py:88`；静态：高级链段/完整Layer不能由顺序修复自动补齐 |
| R03 | 归档依赖闭包、LUT重写和跨OS脱机恢复漏实施 | M20；W10 | `src/features/edit/core/asset_usage_index.py:102`；局部复现：绑定WAV被usage index标unused；不冒充完整归档实测 |
| R04 | 编码输出与sidecar缺统一发布事务和保旧成片验收 | M21.1；W07/W10 | `src/shared/export/video_encoder.py:897`；静态：直接打开最终路径，close异常删除最终路径；序列需集合一致性 |
| R05 | 黑/冻/坏帧与逐版本响度未测状态、QC报告缺生产者接线 | M21.2；W07 | `src/features/edit/ai_editing/delivery_quality_gate.py:40`；静态：字段默认0不能证明已经测量；run_summary有原台账欠项 |
| R06 | 交换子帧、实际采样时基与活动grade版本缺adapter实施包 | M24；M18/W07验收 | `src/features/edit/adapters/fcpxml_adapter.py:639`；计划遗漏/静态字段检索，48k项目输出48k本身不视为bug |
| R07 | 镜头匹配参考锁、实际帧预览、批准与命令前台漏实施 | M22；W07按启用模式验收 | `src/features/color/core/shot_match.py:283`；静态API与台账；现有算法接线与算法升级分开 |
| R08 | COMP增量重跟/修正、人脸/ML Roto前台入口漏实施 | M23；W07按启用模式验收 | `src/features/composite/nodes/face_track_node.py:146`；静态/模型条件项；缺detector不宣称用户功能完成 |
| R09 | Bake worker、产物验证与图rewrite发布协调漏实施 | M08.2；W07/W10 | `src/features/composite/bake_service.py:188`；静态：缓存已有原子存储可复用，不误称所有缓存均不原子 |
| R10 | 评论重复源歧义与Audition fit时长契约不完整 | M15.2/M15.3；W07 | `src/features/edit/core/review_comments.py:197`；评论重复源局部复现；Audition为计划契约缺项 |
| R11 | 取消结果、sample split归属与RGBA外部往返验收遗漏 | M12.1/M14.1/M09/M10；W07 | `src/features/edit/core/verified_ingest.py:69`；取消report.all_passed=True有helper复现；采样/RGBA为已声明范围的验收补项 |
| R12 | CUDA同线程current-context与外部YUV颜色数值门遗漏 | W02/W04；M19共享集成 | `src/shared/media/windows_hw/cuda_resident.py:527`；静态：同线程不保证current-context；RGB/HDR真机偏差本轮未测 |
| R13 | 跨平台证据定位与文件系统命名/目录签名边界遗漏 | M20/W10；两份计划§0 | `src/features/edit/core/project_archive.py:56`；静态规范缺口：平台分隔符/大小写/目标保留名，跨OS真机待验 |
| R14 | 硬依赖过大与W09/UW01推广口径倒置 | M07/M09–M12/M18；W09/升级UW01 | `docs/upgrade_plan_W_windows_2026-10-24.md:1`；计划安排修正，不计产品新bug |

实施与验收责任分开：M18/W07是集成发布门禁，不能把未实现功能仅挂到门禁就算已安排。子任务ID分别登记核验/实施/验收/延期，父包只有在声明范围全部关闭后才关闭；硬件/模型/格式条件只约束对应子项。
跨机交接最少包含：提交与文件白名单、接口/schema差异、fixture/媒体hash、原始证据相对路径、实际运行环境/库hash、未支持范围及回滚。新UI/错误提示走已有i18n与结构化diagnostic入口，控件状态仍防信号递归。

## 修订记录

- 2026-10-24 v1：基于源码、旧Windows真机证据与Mac快速复核，列7项Windows问题、12项共享问题及完成/发布门禁；建立10个Windows工作包和升级门禁。未宣称本次运行过Windows真机，也未修改产品代码。

- 2026-10-24 v2：新增W10跨机脱机往返（主包10→11），补CUDA current-context及RGB/HDR数值门；W09固定候选与UW01动态控制器/默认推广分开，接收M19–M24新增实施与可移植索引。全部工作仍未开始，本轮未跑Windows真机。

- 2026-10-25 v3（Mac 集成者接收）：机器 B 全 11 包经 `36048dfd4` 合并进 fix-ui（M00–M24 与 W00–W10 两线首次同枝）。Mac 侧完整性验证通过（聚焦门禁 GATE PASS + W 改动测试 Mac 全量重跑绿，L3/浸泡类按 Windows-GPU 域诚实跳过）；集成侧两处收口（`5e54134c5`）：①W01 loader 接缝——`dll_matches_current_arch` 对 `.dll` 候选的 PE 解析改为全平台判定（旧实现非 win32 宿主整体短路恒真，B 侧 2 用例在 Mac 失败；machine 对拍仍仅 win32，Windows 行为不变）；②W07 luts 入库缺口——W07 报告称占位 .cube"文件在库中跟踪"，实际被 `.gitignore:1 luts/` 拦截从未入库（干净 checkout 的 apply_lut 工程根解析契约不可命中），按 W07 交接说明改以 Mac 真实契约 LUT（cinematic.cube/07.cube）入库，其余工作 LUT 保持忽略。W07 登记的 S01–S12/黄金链 Windows 分发验收前提自此齐备（M+W 同枝），待 B 机二轮执行。
