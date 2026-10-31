# Windows W00–W10 完成核查（只读，2026-10-25）

基线 HEAD e6c3b6db126574857306b757203de55286d6496d，宿主 Darwin/Python3.12；未运行 Windows GPU、GUI、冻结包或跨机往返。审计依据当前源码、计划、各 W 包报告及入库原始资料；历史报告作为历史证据，不能代填本轮真机结果。

## 逐包判定

| 包 | 当前可确认成果 | 未关闭项/判定 |
|---|---|---|
| W00 | 环境/媒体hash/依赖/旧DLL失败基线有manifest+env JSON | 基线采集完成；计划链接误写12b9987513，实际12b998754；当前HEAD未有B二轮manifest |
| W01 | codec源码构建、12 ABI符号门/PE架构门、Windows二进制写入/句柄/活性锁等修复与历史大组测试 | 组件级完成；报告§6 ctest 92项有51 BAD_COMMAND被策略拒绝；未有冻结包/CLI完整验收，不能宣称全部原生分发门通过 |
| W02 | L1回滚/池预算20用例本轮通过；历史L2/L3和W08 100renderer循环 | 发现initialize未恢复调用者CUDA context；更广尺寸/真实GPU阶段故障注入未完整提供；P1正确性待修 |
| W03 | L1 17用例本轮通过；报告明确真实 UV失败→按源降级→下载planar恢复→后续seek和其他源隔离 | 已有范围的软件修复/历史4090验收可接受；当前合并HEAD仍需复验 |
| W04 | L1几何/设备门17本轮通过；历史420 8/10、非对齐尺寸、422拒绝真机 | 报告:105–121明确W04.2颜色数值后续；多设备/核显为替身；cuGLGetDevices API异常转TypeError残留；不能整包验收 |
| W05 | 历史快照问题定性为QImage测试越界并收紧断言、fence bytes句柄修复及两路由严格对拍 | 软件/历史支持范围完成；本轮未重跑Windows L3，不能冒充当前真机 |
| W06 | extra/打包收集/能力报告、历史干净venv无/有CUDA两态 | 报告:71–73明确冻结exe未build未验；本轮cross-platform测试1失败（WinDLL属性缺）；安装包验收待 |
| W07 | windows-logic required纳入新L1；多个生产缺陷修复；隔离态红基线收口 | 报告:72–81 S01–S12/R系列和三黄金工程全部待M接收；当前虽M+W同枝，无B二轮产物；11项批次顺序污染未解决 |
| W08 | 历史600s/1800s/100重启有JSONL/summary资源趋势 | 页面级multicam/声音字幕代理、多屏DPI、长档未验；脚本当前stop()必TypeError；呈现间隔指标实为submit到frame_ready耗时，不能作为上屏流畅性验收 |
| W09 | 机器B五对×五次组件固定路由有raw JSON，驻留仍opt-in | 干净冻结包/多轨消费者未测；脚本全样本失败仍correctness_stable=True且exit0，seek空结果未判；计时序列循环0..23非连续吞吐。保留旧数值作为特定helper workload，不能据此推广白名单 |
| W10 | Windows名字净化/casefold冲突/目录路径分隔符hash/报告移植等实现；逻辑测试本轮通过 | 报告:66–74明确Mac→Win→Mac脱机像素/PCM/闭包和UNC未测；目标完整闭环未完成 |

## 已复现代码/测试问题

1. **P1 CUDA initialize caller-context未恢复**：`src/shared/media/windows_hw/cuda_resident.py:790–840` 创建/SetCurrent私有ctx后成功直接return；W02 fake driver先置caller，initialize=True但drv.current is caller=False。W02要求initialize/bind/unregister/destroy均保留恢复上下文（`tests/media/test_cuda_resident_lifecycle_w02.py:10–12`），现仅bind/destroy守卫。建议Windows主责补成功/失败初始化上下文恢复、双会话真机+逐阶段失败。
2. **P2 设备查询异常兜底不可达**：同文件:855捕获Exception放gl_err，:857先`int(gl_err)`，:862后安全转换不可达。Fake driver cuGLGetDevices抛RuntimeError后initialize=False，reason变`cuda_init_error:int() argument ... not 'RuntimeError'`。应安全解析再判断；异常不能粗判支持，单/多设备各有清晰拒绝/兜底理由。
3. **P1 Windows安装包审计假通过**：`src/shared/platform/bundle_abi_audit_pe.py:95` consistent只看<=1架构，实测不存在目录/空目录/仅畸形PE均True；畸形有malformed数组仍True。`:72`达max_files后静默不扫，其余坏库可能漏。`scripts/m18_machine_b_recheck.py:95`仅消费consistent导致假PASS。需有效根、有效主exe/PE、全扫描/无畸形、已知且匹配目标架构、codec必需ABI，不满足拒绝。`parse_pe_header`还未验证COFF声明可选头完整性，可独立完善。
4. **P1 B复验脚本门禁语义不足**：`scripts/m18_machine_b_recheck.py:29–41`仅golden_chain3，不是三条；`:85`所有探针仅看exit，探针脚本即使issue_present=True仍exit0；无Windows宿主拒绝/未验状态，缺--dist仍overall PASS。需scope与分级status、探针结构化结果断言、宿主/实际HEAD指纹、skip和未验不可冒充对应门通过。
5. **P2 W08采样线程收尾崩溃（小修候选）**：`tests/edit/test_stress_soak_w08.py:84` Event覆盖Thread._stop方法，:125 join内部调用时TypeError。本轮直接实例start/stop（GPU采样stub）已复现。改_stop_event并补平台无关生命周期focused test。历史有JSONL不抹去；缺原始pytest exit，不能由此断言历史伪造，应当前脚本修后再跑Windows。
6. **P1 W08指标错义及真实呈现未覆盖**：`:261–269`计时submit至frame_ready，`:335`先解码，故不含解码；`:288–290`命名interval_p95，报告称呈现间隔22.1ms；实际5725帧/600s≈9.54fps。`:211–223`仅替Viewer ack，实际空QWidget窗口；不能证明真正Viewer blit/audio时钟/掉帧。需真实页面present timestamp、deadline/重复/过期、端到端延迟与冻结阈值。当前断言只渲染错误为0和显存增长<首半+1GiB（:362–369），无掉提交/seek失败和p95门，2h/10h决定依据亦缺。
7. **P2 W09脚本假绿**：`scripts/bench_ab_routes_w09.py:217–234`无md5/无成功样本默认True，:258永远exit0。Monkeypatch_run_child全no_frame，5/5皆失败仍correctness_stable=True。`:110–113`越界seek返回None不验；24帧生成素材目标180等越界。`:102`i%24循环却称顺序吞吐。需将测过/未测/失败分开，完整样本数/路由命中及seek成功门，正确性外部证据单列；修后Windows重跑并保留历史。
8. **P2 W06测试跨平台失败（小修候选）**：`tests/media/test_cuda_extras_w06.py:75`伪造win32后setattr ctypes.WinDLL默认raising=True，Mac无该属性，失败AttributeError；同类未来补raising=False，维持原断言。

## 证据与文档问题

- 两计划文档头仍v2/全部未开始，与正文勾选和W末尾v3冲突。W全11包x不能代表验收11/11；应区分组件完成/待真机集成/条件延期，首要发布门W07/W06尚未过。
- W00证据路径不存在：计划是`W00/12b9987513/manifest.md`，实际`W00/12b998754/manifest.md`。
- 多W包只存report，未存每条命令stdout、exit、逐失败ID、head/dirty、输入项目hash；W08存summary和JSONL，W09存聚合JSON，但无测试进程exit或运行manifest。应补本轮可复现证据，不改写旧报告为绿。
- `docs/validation/upgrade_2026-10-24/M18/0a6211b70/deferred_gates.md:40–46`清楚记B二轮、冻结包、长档待复开；没有`M18/machine_B/report/`或testfarm对应结果，当前只执行说明/脚本。
- W07批次11顺序污染仍债，隔离通过不等于批次可靠；作为CI维护任务保留，不自动判生产问题。
- UW01动态路由/UW02 AMD Intel/UW03并发交付以及UM路线是未来路线，不能计入本轮已完成主包。

## 本轮focused验证

命令：
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/media/test_topos_codec_loader_w01.py tests/media/test_cuda_resident_lifecycle_w02.py tests/media/test_cuda_resident_capability_w04.py tests/media/test_cuda_extras_w06.py tests/edit/test_resident_failure_recovery_w03.py tests/edit/test_project_archive_w10.py -q --no-header -p no:cacheprovider`

结果：**72 passed / 7 skipped / 1 failed / 3.13s**。唯一失败为W06 WinDLL monkeypatch属性；skip按非Windows/缺CUDA打包条件，不作为真机通过。
