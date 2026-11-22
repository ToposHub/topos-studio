# Topos Studio 专业影视混音全面审计与实施计划

日期：2026-11-22。范围：当前工作区的生产混音页、共享剪辑时间线、实时音频、离线成片/Stem、录音和工程持久化。本文是后续实施的主计划；本轮**只实现 UI/交互**，所有声音后端问题保留为待办，没有用界面改动代替后端修复。

配套材料：

- 本文§20完整收录B10–B20实施手册：监听、录音、AAF、对白、Comp、Reconform、插件、同步、对象音频、硬件与参考声，无需另找一份计划。
- [可运行算法示例](/Users/heng/Documents/vscode/topos-studio/docs/plans/examples/mixer_reference_algorithms.py)、[示例验收测试](/Users/heng/Documents/vscode/topos-studio/docs/plans/examples/test_mixer_reference_algorithms.py)：PDC、跨块延迟、Trim/VCA、签名、QC、响度门限、对齐、交叉淡化、有理时钟和 null test。
- [实际生产代码审计脚本](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.py)、[本次实际输出](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.json)：复现本次发现的声音问题，**不是修复后的验收结果**。
- 历史实施记录：[11-19状态](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-19-film-post-audio-audit-and-roadmap-status.md)、[11-21后端交接与后续修复](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md)、[11-18工作区与Properties状态](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-18-clip-editor-and-mixer-roadmap-status.md)。历史“核心算法完成”不能自动等同于生产全链完成。

## 1. 结论与对标口径

当前产品已有立体声影视混音的主要骨架，首先需要提高复杂路由、自动化和交付的可信度，再扩展外部交接、录音/控制室和原生环绕。增加更多效果器数量不是当前最优先的工作。

对标只采用公开的一手资料，检索日期为2026-11-22；下面是工作流层面的比较，不声称复制某个商业软件全部功能或认证：

| 专业工作流 | 官方参照 | 本计划落点 |
| --- | --- | --- |
| 大规模轨道/Bus、VCA、自动化、Clip FX、Field Recorder、AAF、分轨交付 | [Avid Pro Tools 对比](https://www.avid.com/pro-tools/comparison)、[Avid声音后期工作流](https://www.avid.com/resource-center/avid-audio-post-ecosystem) | B01–B07、B12–B14、B21–B22 |
| 自由Bus结构、ADR、音画同步与沉浸声 | [Blackmagic Fairlight](https://www.blackmagicdesign.com/products/davinciresolve/fairlight/) | B02、B06、B10–B11、B17–B19 |
| 对白/现场录音匹配、改版重整、AAF、录音与空间声像 | [Steinberg Nuendo 功能](https://www.steinberg.net/nuendo/features/)、[ReConform工作流](https://www.steinberg.help/r/nuendo/15.0/en/cubase_nuendo/topics/reconform/reconform_workflow_c.html) | B12–B15、B18 |
| 程序响度和真峰值的算法依据 | [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I)、[EBU R128 v5](https://tech.ebu.ch/publications/r128)、[EBU响度资料及3341/3342](https://tech.ebu.ch/loudness) | B04–B05 |
| 外部插件的延迟、旁路和线程合同 | [Steinberg VST3 Processing](https://steinbergmedia.github.io/vst3_dev_portal/pages/FAQ/Processing.html) | B02、B16 |
| 对象音频数据模型 | [ITU-R BS.2076，当前页面列2076-3](https://www.itu.int/rec/R-REC-BS.2076) | B18 |

Netflix的旧规范链接现跳转到[官方Knowledge Hub](https://studiopartner.netflix.net/studio/branded-sound-mix-spec-and-best-practices)，本次无法从公开页面读取条文。因此本计划不把二手转述的数字当客户要求。实施时取得对应项目当前版本的交付文件，保存来源、版本和测量模式。广播节目、近场对白、院线混音、网络音乐归一化不可共用一个LUFS目标。EBU R128页面明确节目平均目标为−23 LUFS，具体容差、峰值和用途仍应取项目采用版本的条文。

## 2. 当前能力矩阵：避免重复建设

“已有”表示找到生产路径和对应测试，不代表完成全套实机/外部软件认证。“部分”意味着至少有一个模型、算法、命令或消费端尚未闭环。

| 能力 | 当前判定 | 证据与实际边界 | 后续 |
| --- | --- | --- | --- |
| Edit/Mixer共用工程、时间线和Properties | 已有 | 生产Mixer借用Edit部件；Clip Effects、Clip Edit和Properties共用模型/命令/弹窗 | 保持单一系统，UI01 |
| Clip gain、采样级淡化、波形/频谱、非破坏处理栈 | 已有 | `audio_edit`、`clip_pcm`、`pcm_render_cache`、ProjectClipEditor；近两轮已修跨率/预览/声道问题 | B00持续对拍，B08扩长文件 |
| 轨/Bus/Master EQ、FX、Send、侧链 | 已有但组合有风险 | AudioGraphRenderer、AudioMixer及既有路由命令 | B01–B02 |
| Read/Touch/Latch/Write/Off、轨道lane、写保护 | 已有基础 | AutomationRecorder、controller、内嵌lane；内嵌目标排除Bus，没有Master选择 | B03、B07 |
| Trim、VCA、自动化预览捕获 | 部分 | Trim模型/实时接口存在，但离线图漏消费；VCA模型/离线消费存在，生产实时同步未接齐 | B01、B03、B07 |
| 输入分配、多轨预备/录音、录音设置 | 已有 | MixerRecordingController、SharedRecordingInput、RecordingManager | B10–B11 |
| ADR cues、rehearse、提示音、循环、预后卷 | 已有基础 | AdrRecordingController、AdrCue、ADR/Takes面板 | B11/B14完善现场流程 |
| 场次/镜次、最佳take | 已有基础 | Take sheet及controller，不等于完整playlist/comp lanes | B13–B14 |
| 独立监听、Mono/DIM/Mute | 仅输入监听部分 | MonitorSection实际接录音输入块；不是节目统一控制室 | B10 |
| PFL/AFL/Solo-safe、扬声器管理、多cue | 语义字段/缺完整图 | MonitorState有字段，未形成真正tap/solo-safe节目监听 | B10、B19 |
| Stem流式导出、BWF、路由manifest、预卷/尾音 | 已有 | stem_export及相关测试，复杂图仍受PDC影响 | B02、B04–B05、B21 |
| 响度测量/限幅/离线QC | 已有算法，闭环不完整 | 缺数据可QC通过；生产Studio表计仅sample peak/GR，隐藏的shared LUFS表不能当生产界面 | B04–B05、B09 |
| Room Tone、批量交叉淡化、ADR对齐 | 部分纯函数 | dialogue_tools已有；gap split有入口，其他尚未统一命令/资产/撤销链 | B13 |
| FCPXML/OTIO/BWF/CSV | 已有子集 | 以当前capability为准，不能代替AAF重建时间线 | B12 |
| AAF | 明确未实现 | interchange available=False、无生产aaf2适配器 | B12 |
| Reconform | 纯核心原型 | 无生产调用；当前指纹、LCS不足以自动改整部电影 | B15 |
| 5.1/7.1 | 解码、矩阵和容器部分 | layout_capability的track_dsp/bus_master仍stereo，非原生全链环绕 | B06 |
| 高度/对象/ADM | 未实现 | 不能将5.1文件标记或立体声上混称作Atmos | B18 |
| VST3/AU/CLAP宿主 | 未实现 | 内建效果器catalog不是第三方宿主 | B16 |
| 长录音全长Clip Edit | 当前上限300秒 | clip_pcm与pcm_render_cache明确限制；已有窗口解码/预算可复用 | B08 |
| 引擎健康、长时负载、故障恢复 | 部分 | 有ring buffer、generation和诊断核心，audio_health未接生产面板 | B09、B22 |
| 音频freeze/print/bounce in place | 缺生产闭环 | 视频定格和Spectral freeze不算音轨冻结 | B21 |
| 控制台、外部同步、硬件insert | 缺音频专业链 | Tangent调色支持不能算音频HUI/EUCON | B17、B19 |

## 3. 本轮已经直接完成的UI与交互

本轮改动集中在生产 `StudioAudioMixerPanel`，没有改音频引擎、DSP、交付格式或工程schema。

| ID | 已完成 | 使用与验证 |
| --- | --- | --- |
| UI01 | 保留上一轮Properties/Clip Edit/Audio Effects共用链 | 不增加独立效果器状态；已有回归继续运行 |
| UI02 | 常驻通道查找：名称、编号、中文、大小写不敏感、多关键词 | Ctrl/Cmd+F仅在混音台内部聚焦查找；Enter定位；Esc清查找，不拦时间线全局快捷键 |
| UI03 | 全部/音轨/Bus/独奏/待录音视图筛选 | 只影响显示；隐藏通道仍发声/录音/走原路由，Master固定可见 |
| UI04 | 可见/总数、无匹配提示、恢复全部通道视图 | 查找与手动隐藏分开；恢复命令同时解除过滤与隐藏 |
| UI05 | 通道顺序随实际时间线同步，更新通道编号 | 重排、删除后撤销及重命名不再保留旧显示顺序；分隔线跟随 |
| UI06 | 全部峰值保持/削波灯复位；单轨点击立即更新数字 | 保留live电平、GR、推子、工程历史和引擎generation；含隐藏通道与Master |
| UI07 | 显式Options入口 | 可发现Compact、Inserts、批量Solo/Mute、Bus创建、录音设置等现有功能 |
| UI08 | 实时路由总览 | 列出所有音轨/Bus的输出、Send电平、pre/post、关闭/静音状态；可按目标查看传递上游 |
| UI09 | 从路由总览进入现有路由编辑 | 双击/按钮发原有track_show_routing，继续走原命令和循环校验；同步后保持有效ID选择，切页关闭清理 |
| UI10 | 窄Dock与可达性 | 两行紧凑工具栏；200/220/320/560px验证；按钮有tooltip/accessible name，中英翻译 |

路由总览属于工程拓扑视图，不能充当PDC诊断、音频到达时间证明或瞬时可听信号追踪。Send的enabled/muted/level决定是否进入其上游关系；音轨Mute/Solo等听感状态仍以引擎为准。

与新后端绑定的未来界面（例如真实控制室、电平历史、VCA编辑、对象声像、AAF预检）在对应任务里给出明确的UI接入合同和验收。后端能力未建立之前，不增加会被误认为可用的空按钮；本轮能独立工作的UI已落实。

主要文件：[生产混音台](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/studio_console.py)、[真实表计](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/console_controls.py)、[路由总览](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/routing_overview.py)、[新增UI验收](/Users/heng/Documents/vscode/topos-studio/tests/mixer/test_console_navigation.py)。controller增加Send显示快照，并修复旧路由UI对不存在的TimelineView.timeline属性及错误窗口parent的依赖，改读get_timeline和真实窗口；继续复用原路由提交/校验，不改变音频引擎、DSP或命令语义。

## 4. 总计划表与依赖

状态：UI01–10 已完成；B00–B04 的核心反例修复与 fixture 基线已于本分支落地
（见§19批次1记录与 `tests/edit/test_b00_counterexamples.py`，各任务剩余边界
在该记录中逐项列出）；B05/B07/B08/B09/B10/B11/B12/B13/B14/B15/B16/B17/B18
/B19/B20/B21/B22 的核心模型/算法/回归亦已分批落地（见§19批次2–6记录）。
B06 全链（原生 track_dsp/bus_master 多声道）、B12 外部接收端往返、
B16 真实插件宿主、B18 ADM 完整 renderer、B22 夜间长压、全部硬件/听音
验收保持待实施。工时为一名熟悉Python/音频的开发者在现有工程上的**粗估有效人日**，含定向测试，不含等待硬件/外部接收端。新手应按里程碑拆小PR，不将估算当承诺。

| ID | 优先级 | 任务/完成产物 | 依赖 | 粗估人日 | 详细方法 |
| --- | --- | --- | --- | ---: | --- |
| B00 | P0 | 可复现参考工程/声音基线/反例回归 | 无 | 3–5 | §6 |
| B01 | P0 | 唯一完整声音快照、变更签名和换图输入 | B00 | 4–7 | §7 |
| B02 | P0 | DAG边级PDC、tap/sidechain、预卷尾音一致 | B01 | 8–15 | §8 |
| B03 | P0 | Trim/VCA实时、成片、Stem、保存重开一致 | B01 | 5–9 | §9 |
| B04 | P0 | QC缺数据阻断与真实文件回读 | B00 | 4–7 | §10 |
| B05 | P0/P1 | 版本化客户profile、测量、M&E/Stem交付包 | B02–B04 | 7–12 | §11 |
| B06 | P1 | 原生mono/5.1/7.1全链、speaker layout与下混 | B01–B02 | 15–30 | §12 |
| B07 | P1 | 专业自动化写入/Trim/preview/capture及所有作用域 | B03 | 8–15 | §13 |
| B08 | P1 | 长录音滚动窗/波形频谱tile/分块离线栈 | B01 | 10–18 | §14 |
| B09 | P1 | 实时图编译隔离、设备线程、健康遥测/欠载记录 | B01–B02 | 10–18 | §15 |
| B10 | P1 | 控制室监听、PFL/AFL/SIP、Solo-safe/校准 | B01、B02、B09；环绕部分B06 | 12–20 | §20/B10 |
| B11 | P1 | 录音时钟、Punch、Poly capture、掉线与恢复 | B01、B09、B10；多声道监听B06 | 20–35 | §20/B11 |
| B12 | P1 | AAF最小可交接子集/外部软件往返 | B01、B04、B05、B08；复杂bake B21 | 25–45 | §20/B12 |
| B13 | P1 | Room Tone、ADR对齐、批淡化、Field Recorder闭环 | B01、B03、B08 | 18–30 | §20/B13 |
| B14 | P1/P2 | Take playlist/Comp/Audition lanes与单次撤销 | B01、B03、B11、B13 | 18–30 | §20/B14 |
| B15 | P2 | 可靠Reconform候选与全工程事务 | B01、B08、B13、B14；尾音打印B21 | 25–45 | §20/B15 |
| B16 | P2 | 第三方插件宿主、扫描隔离、状态和崩溃恢复 | B01、B02、B06、B07、B09、B21 | 单格式beta 40–80 | §20/B16 |
| B17 | P2 | 音频控制台协议与LTC/MTC/时钟同步 | B01、B07、B09、B11 | 控制协议12–20；chase20–35 | §20/B17 |
| B18 | P3 | 对象模型、ADM/BWF、受支持renderer与回读 | B02、B04、B05、B06、B07、B12；hosted插件renderer B16 | ADM子集40–70 | §20/B18 |
| B19 | P2 | 外部硬件insert、多输出/多cue、逐设备校准 | B02、B06、B09、B10、B11；打印B21 | 20–35 | §20/B19 |
| B20 | P1/P2 | 参考mix A/B、监听响度匹配、版本比较 | B01、B05、B08、B10 | 12–20 | §20/B20 |
| B21 | P1/P2 | Printmaster、Freeze/Bounce in place和可逆恢复 | B01–B05 | 8–15 | §16 |
| B22 | P1贯穿 | 大工程性能、恢复、兼容、安全归档与发布门 | B00；随各任务持续执行 | 8–15+ | §17 |
| B23 | P1/P2 | 内建效果器剩余能力、Clip侧链/遥测、资源/高级DSP | B01、B02、B05；布局B06/自动化B07 | 基础10–20，进阶另估 | B23详表 |
| B24 | P1/P2 | 对白修复、环境匹配、可懂度与降噪增强 | B01、B04、B08、B13；实时B09 | 基础12–25，ML另估 | B24详表 |

建议顺序：B00 → B01/B04 → B02/B03 → B05 → B07/B08/B09 → B10/B11/B12/B13/B20/B21 → B06/B14/B15/B19 → B16/B17 → B18。立体声闭环可以在B06之前交付；原生环绕验收必须等B06。

不要开启二十多个同时实施分支。每次选择一项中的一个可验证小步骤，先形成失败回归，再改生产路径，最后跑关联组。

## 5. 新手实施准备与统一完成标准

### 5.1 环境与第一天的动作

1. 在项目根目录工作，先读取`.clinerules`、AGENTS.md及本计划；保留当前未提交改动，不执行reset/clean。
2. 用`git status --short`识别自己的改动；为单项工作建立`codex/`前缀分支。若工作区有他人改动，使用独立worktree，不把他人改动混入提交。
3. 使用项目已有`.venv`与锁定依赖。先跑下面的算法例子和定向基线；缺环境时按项目现有bootstrap文档修环境，不能删断言让测试通过。
4. 先阅读目标模块已有测试和调用者，再添加一个最小失败案例。算法oracle写在测试里，不从生产函数反向计算期望值。
5. UI只接现有service/command。新声音字段同时补不可变模型、to/from dict、未知字段保留、迁移、command、undo/redo、实时、离线、缓存键和预检。

```bash
# 以下命令均在项目根目录执行；Windows设等价的QT_QPA_PLATFORM环境变量。
.venv/bin/python -m pytest docs/plans/examples -q
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/2026-11-22-mixer-professional-audit.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_audio_graph_snapshot.py tests/edit/test_audio_signature_completeness.py \
  tests/edit/test_pdc_and_stem_sum.py tests/edit/test_mixer_pdc_alignment.py \
  tests/edit/test_automation_recorder.py tests/edit/test_master_automation.py \
  tests/edit/test_delivery_preset.py tests/edit/test_delivery_qc.py \
  tests/edit/test_monitor_section.py tests/edit/test_vca_groups.py \
  tests/edit/test_dialogue_tools.py tests/edit/test_reconform.py
```

审计脚本当前用退出成功表示“成功收集反例”，不表示引擎通过。修复时把JSON中的期望转成真正pytest断言，新增测试应在修复前失败、修复后通过；保留这份历史证据。

### 5.2 每项必须交付的文件与验证

- 一个最小失败fixture及真正生产入口测试；涉及UI时用真实鼠标/键盘，不能只emit目标信号。
- 模型/命令/消费链变更，注释说明单位、坐标域、版本和边界；不得另建UI状态权威源。
- 旧工程、保存重开、undo/redo、锁轨、缺素材、失败/取消、换工程/关闭的验证。
- 实时/成片/Stem使用同一声音快照；导出pending/failed/cancelled时不给成功文件或报告。
- 记录测试命令、版本、fixture seed、采样率、帧率、通道顺序和客观结果；人工验证另标“未执行/通过/失败”。
- 按AGENTS工作流做Explore→方案→实现→Review→修复→验证。没有“基础测试绿了就自动完成整项”。

### 5.3 术语与单位

PCM在现有核心通常是planar `(channels, samples)` float32；时间坐标必须带域。Clip处理栈使用48 kHz参考样本，设备/输出使用自己的采样率；sequence frame使用有理fps。音量线性倍率与dB分开，`0.5`线性约为−6.02 dB，不能当−0.5 dB。sample peak是采样点最大值，true peak是重建波形峰值，两者不可混用。PDC是算法延迟补偿，不是混响的物理pre-delay或音乐性delay时值。

## 6. B00：参考工程、声音基线与反例固化

**入口**：[审计脚本](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.py)、[三端对拍测试](/Users/heng/Documents/vscode/topos-studio/tests/edit/test_audio_three_way_parity.py)、[Stem测试](/Users/heng/Documents/vscode/topos-studio/tests/edit/test_stem_export.py)。

实施步骤：

1. 使用固定seed生成可公开WAV：每声道one-hot脉冲、1 kHz正弦、50/60 Hz嗡声、粉噪/带限噪声、短点击、立体声反相和对白替身包络。真实对白另作本机人工fixture，不提交用户素材。
2. 建立10分钟参考工程：DX/ADR/Foley/FX/MX普通轨，DX Bus、FX Bus、reverb return、Master；包含pre/post Send、侧链、自动化、一个有延迟效果、一个长尾效果、非零源入点、采样淡化。
3. 所有源采用asset ID/相对资源URI和指纹；fixture脚本存入测试支持目录，临时工程由pytest tmp_path创建，不能依赖开发机绝对音频路径。
4. 把本次五类反例拆成独立测试：串联Bus PDC、Trim渲染、逐声音字段签名、缺测量QC、VCA+Read曲线。不要把错误的当前输出存成golden。
5. 保存一个旧版本小工程和已确认正确的浮点参考输出；记录旧算法版本及允许改变的范围。修PDC会改变有问题工程的声音时序，发布说明要明确。
6. 统一输出比较工具：先对齐起始样本、布局、长度、PDC和预卷，才计算max error/RMS/null；AAC等有损输出单列测编码延迟和容差。

算法示例：`null_error`和`sample_at_frame`在可运行示例中。使用绝对边界：

```python
start = frame * rate * fps_den // fps_num
end = (frame + 1) * rate * fps_den // fps_num
block_count = end - start  # 相邻帧共享同一边界；不能每帧round后累计
```

验收：23.976/24/25/29.97 DF/NDF及44.1/48/96 kHz；相同线性图的实时/离线float32对拍建议max error≤2e−6，24-bit PCM回读建议≤2e−6，具体按运算顺序和量化设明确阈值。不能为了通过测试自动平移候选音频隐藏PDC错误。非线性Master后的分类Stem不要求数学求和等于Full Mix，必须标取点。跨块边界误差≤1输出样本；长片时钟累计边界无漂移。

完成：fixture不含私有素材；五类反例有断言；10分钟工程可生成/保存/重开/导出；每个golden有来源与算法版本。

## 7. B01：完整声音快照与统一失效机制

**现状/反例**：EditPage `_build_audio_timeline_signature`漏Send.tap/muted、Trim、VCA等；AudioGraphSnapshot.stable_hash也不是完整声音身份。相同工程revision不保证有效声音缓存已失效。

**入口**：[EditPage](/Users/heng/Documents/vscode/topos-studio/src/features/edit/ui/edit_page.py)、[AudioGraphSnapshot](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_snapshot.py)、[签名测试](/Users/heng/Documents/vscode/topos-studio/tests/edit/test_audio_signature_completeness.py)。

实施步骤：

1. 列出所有实际改变声音的字段，用表标owner、单位、版本、实时消费者、离线消费者、cache key。至少覆盖source fingerprint/stream/channel patch、源入点/方向/retime、sample offsets、gain/fade/curve、audio_edit、Clip FX及动画、轨/Bus/Master EQ/FX、路由/Send.tap/mute、所有自动化/trim/mode、VCA、layout、采样率、PDC算法版本、外部IR/插件state hash。
2. 提取版本化的不可变`SoundSnapshot`，区分声音身份、工程身份和运行请求。声音hash不包含选中片段、Dock宽度、播放头等UI；工程ID/sequence ID/generation仍必须在异步发布校验中，避免同声不同工程回包串写。
3. 只在worker提交之前解析模型；worker不通过绑定方法反读正在改变的controller。资源只携带稳定URI和验证指纹，解码器/PCM缓存等运行态不序列化进工程。
4. 将已有实时signature、预渲染key、离线renderer输入接到同一显式字段投影。routing validation快照与声音快照可以共用子结构，不能在未补齐字段时直接将旧stable_hash升为唯一键。
5. 给key加入schema_version和各DSP algorithm_version。旧磁盘缓存按旧前缀失效/惰性回收，不能沿用同名文件。
6. 在engine块边界接受候选图，并带完整身份发布计量和预览；旧generation结果只丢弃并释放，不能改新面板状态。
7. 将所有UI提交路径继续保留原command；不要在UI手动清缓存作为唯一失效补丁。
8. 路由整体output+sends应组成一个原子宏事务：先校验候选完整图，再提交模型/换图，失败零修改、一次undo恢复。当前controller逐条提交且preview/rollback/commit向引擎固定传is_bus=False，需按真实track.is_bus保持节点身份，并覆盖Bus发送/取消/历史/重开。此为代码检查发现的待修后端合同，本轮仅接通UI，没有修复这两点。

建议结构（示意，字段清单必须按步骤1补齐）：

```python
payload = {
    'schema_version': 2,
    'sample_rate': 48000,
    'layout': ['L', 'R'],
    'clips': [...], 'tracks': [...], 'buses': [...],
    'master': {...}, 'vcas': [...], 'algorithm_versions': {...},
}
# 完整payload由模型适配器构建，不能直接对任意__dict__序列化。
key = sound_hash(payload)  # 示例模块；allow_nan=False，稳定key排序
request_identity = (project_id, timeline_id, key, generation)
```

验证方法：对上述每个字段做“只改一项”的参数化测试，断言hash改变、首次新块生效、离线结果对应；对Dock/选中状态做不变对照。A慢/B快/A→B→A、失败→重试、缓存同步命中都不能混身份。保存重开同声字段hash一致，未知扩展字段按工程兼容策略保留。

失败与迁移：未知DSP版本/坏URI/NaN参数阻断提交或交付并指出clip/effect ID；旧项目用显式迁移默认，不能给新字段随意填听感不同的值。新增schema单测不能覆盖掉旧文件里的其他feature payload。

完成：一个维护中的声音字段清单；签名变异全覆盖；所有消费端使用同一快照；旧缓存不会误命中。

## 8. B02：DAG边级延迟补偿与共同调度

**反例**：dry直达Master，wet经100样本B1→200样本B2，同源sample100脉冲当前出现在`[100,500]`，声明PDC200。正确应两路都在sample400汇合、幅度2，算法延迟300。详见审计JSON。

**入口**：[离线图](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_renderer.py) `_recompute_pdc`、[实时图](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_playback.py) `_recompute_pdc_locked`、[现有PDC回归](/Users/heng/Documents/vscode/topos-studio/tests/edit/test_pdc_and_stem_sum.py)。

实施步骤：

1. 从路由验证后的图展开节点：Clip FX、轨pre-fader tap、fader、post-fader tap、各Bus/Master、detector sidechain。Send只能连到它实际取样的tap，不能全按轨末尾延迟算。
2. 每个DSP报告intrinsic latency，tail单独报告；创意Delay的delay_ms和混响pre-delay不是自动抵消的算法延迟。旧效果和新插件均需明确合同。
3. 拓扑排序拒绝未知目标、自环、Bus环和sidechain依赖环。当前不引入带反馈的路由；需要反馈延迟另立安全合同。
4. 按最长输入到达时间生成**每条边**的补偿，不再以“轨max+Bus max”两级近似。Master参与汇合；一个源的多个Send有独立edge ID/延迟线。
5. 编译阶段分配有上限的delay ring buffer，音频callback只读计划并推进；延迟超预算时报错，禁止按用户参数分配无界内存。
6. 实时、成片、Stem消费同一`PdcPlan`。范围导出先从共同时间轴预卷DSP/自动化，再只写交付范围；全图起点、负时间静音、源handle和尾音策略明确。
7. 旁路/换采样率/插件延迟变更重新编译图。对齐旧新延迟后交叉过渡；seek和stop清延迟状态，尾音drain不能重复输出或截断。
8. 特别覆盖Bus路由预览、提交、取消时节点is_bus身份和Send端点不变，完整路由事务由B01提供；UI总览的正确显示不能证明设备图正确。
9. 修复后更新工程声音兼容说明与算法版本，保留明确“按旧版本打印”的历史音频资产，不默认强行兼容错误时序。

核心算法（完整带环验证版本见`plan_pdc`示例）：

```python
# latency[u] 是节点u实际DSP带来的固定样本延迟。
arrival[v] = max((arrival[u] + latency[u] for u in inputs[v]), default=0)
edge_delay[u, v] = arrival[v] - arrival[u] - latency[u]
# 所有edge_delay >= 0；在源到目标汇合前用流式延迟线应用。
```

`ReferenceDelay`示例验证块长变化，不可直接放callback：其中np.concatenate会分配。生产版复用既有延迟缓冲，预分配环，write/read index按实际样本数更新；flush时补零，不能重复推进源时间。

验收矩阵：直达/单Bus/两级Bus/并联Bus、pre/post Send、侧链、Clip FX延迟、Master lookahead分别组合；块长1/127/256/733/2000；脉冲位置完全一致，随机切块与整段最大误差≤2e−6。范围导出与整段切片比较，含长reverb/delay尾、pause/seek/旁路/换图及取消。侧链测试应证明目标GR发生在同一绝对样本附近，不只看主路脉冲。非线性算法允许自身数值容差，但不允许补偿错位。

完成：实时/离线不再各算近似PDC；latency/tail分别可诊断；所有组合及资源释放测试通过。

## 9. B03：Trim、VCA与三端一致

**反例**：EditTrack volume trim为0.5，模型求值已0.5；AudioGraphRenderer对输入0.5仍输出0.5，预期0.25。VCA override接口有定义但没有生产同步调用；另外即使手动设override=0.5，加入Read模式volume恒1曲线后，输入0.5从预期0.25回到0.5，ramp支路漏乘VCA。不要新写一套VCA模型。

**入口**：[模型与VcaGroup](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_v2.py)、[AutomationRecorder](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/automation_recorder.py)、[实时混音](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_playback.py)、[离线图](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_renderer.py)。

实施步骤：

1. 写清gain staging合同：源/retime→audio_edit处理栈→clip fade/gain→Clip FX→轨链前pre-fader tap→fader/pan/panner（包含自动化×volume trim×VCA）→strip EQ→track FX→现有post_fader Send tap→输出mute/PDC。当前post_fader的实际语义包含轨FX之后，不能误插到推子后、EQ/FX前。本步骤先用现有顺序，若要改顺序必须版本迁移，不能借机改变旧工程。
2. 本项仅接现有volume trim线性倍率。模型泛型get_automation_value目前对参数应用乘法，而实时audio_playback明确仅volume参与trim，pan/mute没有统一trim合同；pan/FX trim作为B07版本化新语义设计，不称为现成的加法能力，不将通用乘数套全部参数。
3. `_TrackNode`快照携带Trim并在采样自动化求值中实际消费；Bus/Master有各自合法字段，缺某作用域时显式能力不支持。
4. 在生产模型→manager同步中接VCA overrides，volume标量与已求值automation ramp均乘同一VCA factor，处理删除成员、重开、换sequence、undo/redo。不要只在用户拖VCA时临时写manager。
5. 给VcaGroup补正式command：建/改/删组、成员、gain/mute、自动化；默认平级组，嵌套需要无环校验和唯一增益计数合同。
6. 不改routing，不将VCA当音频Bus；pre-send不应随fader/VCA移动，post-send应跟随；mute/solo-safe与监听规则交B10统一。
7. UI通过模型/command显示组和成员；后端通过本项前不开放可听感误导的VCA控件。

示例：

```python
# 既有模型volume trim是线性值；VCA例子是dB。
gain = base_gain * trim_factor * 10.0 ** (sum(vca_db_values) / 20.0)
# input 0.5、trim 0.5、VCA -6.0206dB -> output约0.125。
# muted时输出0；不要用-60dB冒充真正静音。
```

验证：普通轨/Bus/Master、多个VCA成员、mute、删除、重新加组；自动化0→−12 dB穿过块边界，比较实际PCM。固定参考时间点检查预/后Send倍率。保存重开和undo/redo后三端完全恢复；没有组/trim unity的旧工程声音不变。可运行示例`gain_with_trim_vca`提供单位oracle，生产测试调用真实manager和renderer。

完成：反例恢复0.25；B01全部相关签名变更；一个command只占一步undo；三端和重开对拍通过。

## 10. B04：QC缺失数据阻断和文件回读

**反例**：仅`{'integrated_lufs': -23}`即可让当前`evaluate_delivery`通过。缺true_peak/sample_peak生成NaN后，`NaN > ceiling`为False，漏检。

**入口**：[交付预设](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/delivery_preset.py) `evaluate_delivery`、[QC](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/delivery_qc.py)、[响度分析](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/loudness_analyzer.py)。

实施步骤：

1. 定义`MeasurementResult`：status、file hash、sample_rate、layout/channel_order、start_sample/count、algorithm/version、gate_mode、measurements、warnings/error。区分pending/complete/silence/failed/cancelled。
2. 由profile声明每个交付item需要哪些测量；非数字、bool、缺字段、NaN/Inf、过期hash和范围不匹配均阻断。静音的LUFS−∞应转换为结构化silence，而不是用任意小数假装测得。每item由profile生成required集合；允许静音的FX/ME须status=silence、sample count>0、布局/文件hash/范围正确且allow_silence=True，才走明确静音验收。院线或无LUFS目标的item不强制programme LUFS，但仍要求其profile规定的峰值/文件检查；禁止全局放行−∞/NaN。
3. 测量worker读取**实际写出的最终文件**，含重采样、limiter、编码/量化后的结果；先测内部float可以用于预检，但不能代替最终回读。
4. 给导出产物使用临时目录。编码完成→关闭flush→回读→测量/QC→生成checksum/manifest；失败状态不能有`passed=true`，也不能把半文件重命名成正式交付。
5. 对要求必须合规的Full Mix设置blocking；某些FX/ME允许长静音或无需整体LUFS目标，规则由B05逐stem规定，不能所有stem强行归一化。
6. 报告中定位超限时段/声道/指标，保存计算参数。取消/缺后端/损坏文件标记原因并清临时产物；不静默转sample peak冒充true peak。

关键防线（仅示范profile要求节目LUFS目标的非静音item）：

```python
import math

def required_measurement_error(record: dict, required: tuple[str, ...]) -> str | None:
    for key in required:
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            return f'{key}: missing or invalid'
    return None

required = ('integrated_lufs', 'true_peak_dbtp', 'peak_dbfs')
assert required_measurement_error({'integrated_lufs': -23}, required) is not None
# guard通过后才比较限值，仍需校验status/hash/range；完整示例见validate_measurement。
```

验收：逐字段缺失、None/字符串/bool/NaN/±Inf、测量未完成、静音、空文件、截断WAV、坏layout、取消、无SciPy/ffmpeg。只有符合profile且绑定当前文件checksum的complete结果能通过。真峰值与独立参考测量器对拍目标≤0.2 dB、响度≤0.1 LU，最终阈值按官方测试信号/选定算法版本固定。测试要包含采样点未超限但采样间超限的高频信号。

完成：原QC反例阻断；每个失败有原因；交付报告和文件一一绑定；不完整产物不进入正式包。

## 11. B05：客户规范、专业计量与交付包

**现状**：有响度算法、preset、Stem/BWF/manifest；内置“流媒体−14”“影院−27”不具备通用规范效力。生产Studio strip只有sample peak/GR，不是完整节目响度表。

**入口**：[loudness_meter](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/loudness_meter.py)、[loudness_analyzer](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/loudness_analyzer.py)、[delivery_preset](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/delivery_preset.py)、[stem_export](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/stem_export.py)、[BWF](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/bwf.py)。

实施步骤：

1. 新profile结构包含稳定ID/schema、客户/用途、来源URL或文档hash、版本/获取日期、programme/dialogue/ungated模式、目标/容差、true peak、测量范围、声道权重、文件规格和每stem规则。用户自定义需标“客户自定义”，未核实模板标“示例”。
2. 旧preset继续可读，但标legacy/unverified，不能静默替换旧项目的响度目标。迁移记录旧数值；影院profile允许没有LUFS目标，改为客户约定和监听校准要求。
3. 复核BS.1770 K-weighting有状态滤波与通道权重、LFE排除、400ms/100ms programme gate；Momentary/Short-term/Integrated/LRA/True Peak分开。对白gate依赖可靠对白检测或批准的时间段，不能拿DX轨整段LUFS贴“dialogue gated”标签。
4. 同时聚合主输出的声道相关/极性、M/S和mono兼容诊断，算法必须按布局和实际tap发布，不能用装饰性goniometer代替真实数值；与Stereo Imager既有指标复用。SRC质量用脉冲/扫频测passband、阻带、alias和跨块相位，离线交付不使用未经验证的线性插值替代高质量重采样。
5. 在线计量在worker累计，采样块/seek generation一致；UI每15–30Hz接聚合快照，不在paint里滤波/FFT。节目I/LRA reset与UI06峰值reset是不同动作，明确按钮和状态。
6. 全节目离线测量可取消/可重试；保留range而不是默认测当前播放过的部分。实时在线数值不能替代完整节目交付检查。
7. Full Mix、DX、ADR、Foley、FX、MX、M&E建立实际Bus/tap映射，不用轨名字符串猜分类。M&E必须有国际版可用的音乐音效和必要补效，简单Mute台词不足以证明合格。
8. 每stem明确pre/post Master、范围、预卷、尾长和共用起点。非线性Master对“独立处理stem再求和”和“Full Mix分类贡献”给出不同语义，不宣称两者完全相等。
9. WAV/BWF/RF64以实际字节预算选择，明确24-bit/float、dither仅在最终降位深时一次执行。写入bext起始sample reference、fps/DF/声道标签及iXML（若实现需版本化）。
10. 按客户profile添加同步leader、头尾2-pop及参考画面烧录TC；定义是否在交付测量range内，不能通用硬编码或混进所有stem。
11. 生成delivery manifest：项目/sequence/revision、sound hash、客户profile版本、每文件sha256/样本数/layout/measurements、路由图、插件版本和未支持项。交付包在所有必需item通过后原子发布。

重要算法：`gated_loudness_from_k_energy`示例只演示双门限，输入必须是**已完成K滤波和布局加权**的400ms能量。不要直接传原始RMS：

```python
absolute = energy[energy > 10 ** ((-70.0 + 0.691) / 10)]
relative = absolute.mean() * 0.1  # 相对门限−10 LU，在能量域计算
accepted = absolute[absolute > relative]
I = -0.691 + 10 * log10(accepted.mean())
```

无有效块返回silence。True Peak要用连续状态的重建/多相滤波并覆盖块边界；不能对每个小块独立插值再取max。参考现有算法和官方向量验证，不重复发明滤波系数。

验证：官方响度测试信号、K滤波频响、静音/短节目、不同块长、多声道LFE-only、−3dBFS mono与双mono区别。Full Mix设目标并不意味着FX/MX都同目标。回读检查所有文件共同sample0、count、TC、order、checksum，测试>4GiB产物及磁盘满。客户/接收端确认报告，未确认不得标认证。

完成：通用假标准移出默认合规列表但保持旧项目可读；生产计量字段来源可追溯；真实文件QC和交付manifest闭环。

## 12. B06：原生多声道，先5.1/7.1后高度

**现状**：source/clip支持多声道，Bus矩阵部分支持；track DSP和Bus/Master仍立体声。不能只改下拉框让5.1通过。

**入口**：[layout_capability](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/layout_capability.py)、[channel_layout](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/channel_layout.py)、[audio_graph_renderer](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_renderer.py)、[audio_dsp](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_dsp.py)。

实施步骤：

1. 写layout合同：稳定speaker IDs和顺序；mono、stereo、5.1、7.1分别表示；codec order↔内部order显式矩阵。LFE不是第六声道编号猜测，不把mono声道复制成假环绕。
2. 在模型/graph snapshot携带source、clip patch、track、Bus、Master、device/export各layout。为旧项目设明确stereo默认，保留原有source mapping。
3. 把硬编码2ch处理替换为布局感知buffer。简单gain/delay逐通道；动态效果定义linked groups；M/S、stereo imager等只作用兼容布局，缺能力时报阻断或用户明确选择downmix处理。
4. 每个effect catalog声明supported layouts、输入输出转换、latency/tail、自动化。未知第三方插件layout不能自动裁声道；B16协商失败应提示。
5. panner复用现有audio_dsp.surround_panner_gains及vbap.py的离线数学核，接通mono→stereo与mono→水平环绕的生产图，扬声器方向取设备layout；对象位于两个speaker间解非负gain并归一化能量。stereo source宽度/多通道bed采用单独规则，不能将同一mono公式套全部。
6. 下混使用现有矩阵并校验BS.775规则/客户需求，LFE默认策略显式选择；bass management只在监听链执行，不能等同于delivery downmix。
7. 多输出设备按channel patch直接送对应speaker；不可用硬件时提供明确stereo监听折混，输出文件仍按原生layout导出。
8. 一次只开放完成解码→DSP→Bus→Master→输出→WAV回读的layout，7.1.4/对象交B18。

矩阵/声像参考：

```python
# y = M @ x；x是(C_in,N)，M是(C_out,C_in)。
# 双扬声器VBAP二维小例，speaker角分别−30°、+30°，目标0°。
import numpy as np
angles = np.deg2rad([-30.0, 30.0])
S = np.stack([np.cos(angles), np.sin(angles)])
g = np.linalg.solve(S, np.array([1.0, 0.0]))
assert np.all(g >= 0)
g /= np.linalg.norm(g)  # [sqrt(.5), sqrt(.5)]；不能直接当含LFE全布局算法
```

验证：每声道独特sample位置/频率one-hot；布局roundtrip、串音<−100dB（浮点直通）、LFE策略、panner中心等功率、跨speaker边界连续、downmix一致。真实6/8输出设备逐speaker听测标注，外部播放器回读order，未执行实机不得标原生硬件验收完成。

完成：capability矩阵与实际一致；旧stereo声不变；5.1/7.1全链不被隐藏降混；设备与文件回读通过。

## 13. B07：专业自动化写入与可编辑范围

**入口**：[automation_recorder](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/automation_recorder.py)、[AutomationLane](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/automation_lane.py)、[生产时间线UI](/Users/heng/Documents/vscode/topos-studio/src/features/edit/ui/timeline_panel.py)、[自动化controller](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/edit_controller.py)。

实施步骤：

1. 保留已接通Read/Touch/Latch/Write/Off和现有recorder；画状态转移表：开始播放/触摸/释放/停止/seek/切页/关工程如何处理。Write不能在播放停止后继续覆盖。
2. 为每个可自动化参数使用稳定parameter ID、单位、插值、作用域/最小最大值，volume dB显示与线性求值保持对应。FX结构变更与参数自动化ID分开。
3. 扩展同一command与lane目标到Track/Bus/Master及已声明FX，包含pan、Send level、effect params；选择器只列真实capability，不假造任意参数。
4. 一次手势先采preview缓冲，按实际音频时钟sample/frame记录；松手/停止后合并成一条command。Touch释放按return time回到原曲线，Latch写到stop，Write按明确range覆盖。
5. preview/capture/punch/write-to-selection/start/end和write protection走同一临时状态机。自动化预听不写工程，capture只存合法快照；commit时重新核对revision和锁轨。
6. Trim用B03已有模型；coalesce把trim烘入曲线后复位trim，保证同一时间声音不变。不要累计不可逆偏置。
7. 自动化follow edit需在移动/裁剪/涟漪/reconform事务中统一移动时间；多选复制与单次撤销保留被覆盖range外曲线、边界节点和插值。
8. 节点简化保留误差界。volume在dB域定误差（例如≤0.1dB），pan归一化误差≤0.005，hold/陡边/触摸边界不能被RDP删去。

范围写入算法：取提交前快照，在`[start,end)`删除待覆盖节点，保留start之前和end之后；用原曲线分别在start前一个可表示坐标与end求值补边界，再插入新采样。现有Bezier/hold必须保留切分语义，不能仅过滤frame列表造成边界跳变。subframe能力未开放的lane继续frame域，写入误差按公开能力标注，不能声称sample-accurate。

验收：同一手势一步undo；Touch/Latch/Write各10个状态转移；播放中stop/seek/切页/取消；锁轨/写保护无修改。Trim/coalesce前后PCM null、所有作用域重开和成片/Stem对拍；1000节点编辑p95目标<50ms（指定机器实测，未测不承诺）。

完成：没有重复recorder；全部作用域可读可写可撤销；边界/模式/单位测试齐全；UI基于真实capability显示。

## 14. B08：长录音、滚动窗口与分块处理

**现状**：窗口读取和预算已有，`MAX_CLIP_PCM_SECONDS`与`MAX_PRE_RENDER_SECONDS`均300秒。把常量调大将暴涨PCM/STFT内存，不是解决办法。

**入口**：[clip_pcm](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/clip_pcm.py)、[pcm_render_cache](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/pcm_render_cache.py)、[ProjectClipEditor](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/project_clip_editor.py)、[clip_preview_worker](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/clip_preview_worker.py)。

实施步骤：

1. 分离`ClipExtent`（完整逻辑范围）和`DecodedWindow`（当前缓存），完整90分钟片段不应因只加载300秒而改变模型时长/导出范围。
2. PCM按source ID/hash、stream、layout、absolute window、rate和algorithm version索引；视窗/播放头前后小量预读，字节LRU按多项目总预算管理，不能每窗口一个无限线程。
3. 波形建立多级min/max/RMS pyramid，按每屏像素取对应level，保留极值；频谱用固定窗/绝对hop格点tile，viewport生成与overview分开，边界补齐上下文。
4. 取消token每个decode/FFT tile检查，切片/缩放/改FFT时旧tile不能写回；UI拖选择只改overlay，不能重算整段STFT。
5. 离线处理栈分为stateful streaming、需要overlap-save、变长global三类。降噪/滤波保留状态；频谱处理重叠加窗并做归一化；WSOLA等维护映射和窗口，不对每块独立reset。
6. 缓存成品落磁盘，metadata有预期sample count、checksum、完整标志和sound key；崩溃留下的partial不当ready。不要将大型PCM写进.topos。
7. 采样级选择、标记、包络与频谱笔迹使用全局clip坐标，tile-local只在显示/计算边界换算，长文件后半段处理能准确提交。
8. 保留当前300秒错误提示直到分块全链验收完成；逐处理器解除限制，不能让不支持的global DSP静默截短。

内存预算例：90分钟48kHz stereo float32裸PCM≈1.93GiB；STFT还会更多。预算应依据`channels * samples * dtype.itemsize`，tile命中不复制整段；合并同键请求。加窗重构验证使用`sum(window_analysis*window_synthesis)`归一化，静音区分母加安全阈值，不能仅叠加导致块边缘音量起伏。

验收：60/90/180分钟生成稀疏WAV尾段读样本，7分钟起点做20ms修复；跨tile选择、seek、重开、变长栈、跨率/多声道。建议64轨90分钟工作集峰值RSS相对稳定预算不线性随时长增长，视窗交互p95<50ms；用固定机器记录实际值。流式与整段oracle对拍≤2e−6（有状态算法按其合同），没有tile接缝爆音。

完成：全长视图/试听/提交/导出范围一致；worker可取消和收尾；每类处理器明确支持的时长，缓存可恢复且内存有界。

## 15. B09：实时稳定性、设备线程与健康遥测

**现状**：已有AudioRingBuffer和generation；仍需核查同步decode/mix fallback，不可宣称完全没有缓冲，也不可只加一个CPU进度条。

**入口**：[实时播放目录](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/realtime_playback)、[audio_playback](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_playback.py)、[audio_health](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_health.py)、[audio_capabilities](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_capabilities.py)。

实施步骤：

1. 用计时/采样分析标callback、producer、decode、graph compile、UI publish边界。禁止设备callback中解码、磁盘、日志I/O、等待锁、Qt对象创建、无界分配。
2. compile worker生成不可变RenderPlan并预分配，设备块边界原子替换；旧图在消费者释放后deferred cleanup，不由UI直接删DSP/设备。
3. 生产线程生成带sample position和generation的PCM块，ring buffer读写处理短写和under/overflow。欠载有独立计数与预定义零填/渐变策略，不虚构播放进度。
4. 设备时钟为播放真源，UI从消费样本位置投影；视频跟随AV sync controller。不同设备采样率/缓冲的切换是事务，确认新设备就绪后转接。
5. 定义`AudioHealthSnapshot`：sample_rate、buffer_size、render p50/p95/p99、deadline miss、ring fill、underrun/overflow、device errors、active sound key、PDC、cache、backend availability。无数据显示unknown，不能显示健康绿灯。
6. 将现有audio_health核心探测移到worker；磁盘walk即便有5秒预算也不能放UI。以可取消快照接生产健康UI/导出诊断，节流更新。
7. 出错时按source/effect/node定位，保留工程；可选降级需用户明确选择，并让交付继续阻断未知效果。插件crash由B16隔离，不直接杀主进程。
8. 退出/换工程/设备拔插/反复seek覆盖cleanup，线程、句柄、PCM、环缓冲、临时文件均有owner。优化先测量，确有瓶颈再迁native，不能为“专业”无依据重写整个引擎。

预算算法：buffer deadline_ms=`1000 * block_samples / sample_rate`；256@48k≈5.33ms。至少记录p99和最大值，平均CPU低不能证明不爆音。建议render p99<deadline的70%，长时deadline miss=0（固定fixture机器），显示buffer余量可解释欠载。

验收：64/128轨分别含FX/Bus/自动化，48/96k，buffer128/256/1024；连续60分钟、编辑时播放、100次seek、20次切工程/设备，掉线/失败/磁盘慢注入。UI p95<50ms、无死锁、线程/句柄数回到基线，记录实际硬件/系统；物理听感检查爆音另列，离屏测试不替代。

完成：callback职责清晰；所有失败有遥测；健康UI只读真实数据；worker/设备资源退出可证明。

## 16. B21：Printmaster、Freeze与Bounce in place

**入口**：[共享renderer](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_renderer.py)、[Stem](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/stem_export.py)、[缓存](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/pcm_render_cache.py)。

实施步骤：

1. 区分freeze（性能缓存，可解冻回原链）、bounce（生成可追溯新asset/clip）、printmaster（固定revision的交付打印）。三者共用renderer，不新增独立混音算法。
2. `RenderArtifact`包含project/sequence/sound hash、范围/layout/rate、tap、PDC/tail策略、source dependencies、文件URI/hash、ready/failed/cancelled状态。Freeze metadata引用原链完整快照，禁止删除原FX参数。
3. 先按选定tap渲染到临时资产并B04回读，成功后一个command替换/关联来源；取消/失败工程零变化，undo恢复原链，redo复用仍有效成品。
4. 定义依赖失效：源重连/效果或自动化变化、sidechain源变化、Bus上游和route变化都会使相关freeze过期。跨工程使用的成品是正式asset，不因清缓存被删。
5. Send与sidechain决定freeze边界。首版只允许独立轨pre/post-fader的明确范围；依赖外轨的链要包含依赖快照或拒绝，不能冻结出不同声音。
6. tail有明确最大时长/阈值，文件起点/handles和原clip offset对应；新clip需要长度事务处理链接A/V/锁轨/ripple。
7. printmaster revision在整次作业锁定，UI继续编辑生成新revision不影响正在输出的旧快照，界面显示本次输出revision。

验证：freeze前后同tap PCM null、CPU下降实测；变更依赖变stale；unfreeze/undo/redo/save/reopen正确。坏成品/丢文件回到可重建状态，项目归档包含成品和必要原始资产；清理缓存不会删项目资源。非线性Master多stem仍按B05语义。

完成：缓存和正式asset生命周期分开；可恢复；没有静默失效或不可逆替换。

## 17. B22：大工程、灾备与发布验收

实施步骤：

1. 生成10/30/90分钟，32/64/128轨，合理clip数、Bus深度和自动化点的固定seed工程；性能目标标注CPU/内存/OS/设备和buffer，不拿无FX空轨代替负载。
2. 文件资源用稳定ID/相对URI和checksum，Collect/Archive收集源、IR、录音take、插件state、冻结成品，排除可重建临时PCM；缺依赖预检显示确切ID。
3. 工程保存与manifest采用临时文件→flush/必要fsync→原子replace；录音recover journal记录已落盘块，不能对实时每块fsync拖死callback。
4. autosave保留版本和依赖清单，恢复到明确revision；音频录制中崩溃/磁盘满/权限变化不破坏上一版工程。用户确认后导入可恢复take，不能自动拼接未知时钟段。
5. 新schema用旧版本golden测试迁移，未知feature payload不丢；共享命令撤销只影响当前sequence，不把录音设备ID/secret/机器绝对路径写成跨机依赖。
6. 客户交付资料、真实素材和设备名称可能敏感，诊断包默认剔除完整路径和媒体内容，保留可复现的fixture ID/算法版本。
7. CI做纯算法/命令/文件格式回归；夜间做长时压力；发布候选在macOS/Windows各真实设备和两套外部后期软件做交接验收。新手可负责自动化组，不能替代工程师听音和客户确认。

验收/完成：30次保存重开不改变sound hash；100次撤销重做返回原始PCM；故障注入不产出伪成功文件；90分钟项目内存不随循环播放持续增长；所有未完成实机项在报告列明。

## B23：内建效果器的剩余能力与统一生产合同

优先级P1/P2。依赖B01/B02/B05，Clip侧链依赖B02，环绕扩展依赖B06，项目速度图依赖B07。预计10–20人日完成能力/资源/遥测闭环；图形音高校正与实时和声等创意扩展各15–30人日另估。

不能重启一份“24款效果器全部重写”的任务。当前已有多段Dynamic EQ、多段压缩、De-Esser lookahead、卷积worker、TPDF最终量化、瞬态/调制/离线Pitch/Doubler等，历史文档前段的待办已有不少被文末补交付完成。对照[最终完成度表](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-21-audio-fx-delivery-record.md)及[原详细算法手册](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-21-audio-effect-panels-backend-handoff.md)时，以当前代码和新测试决定是否仍需实施。

本轮核查入口：[参数映射扩展](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/effects/production_extensions.py)、[生产弹窗](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/effects/production_dialog.py)、[已接控件回归](/Users/heng/Documents/vscode/topos-studio/tests/mixer/test_audio_fx_completed_controls.py)。仍有未消费的原型高级控件；例如saturation auto gain/tape高级区在生产适配中明确禁用。它们是后端能力任务，不靠取消禁用获得功能。

| 子项 | 真实剩余范围 | 具体实施与验收 |
| --- | --- | --- |
| E01 EQ/Clip/实例遥测 | Track/Bus/Master已有PCM订阅；Clip完整宿主分发、工程身份仍需闭环 | 发布`project/timeline/clip/effect/generation/sound key/sample range`；仅当前订阅收包，关闭/旁路/seek清窗。两个同类实例输入不同信号时必须显示各自频谱；复用已有有界队列与FFT worker |
| E02 Clip外部sidechain | Dynamic EQ/Dynamics/Transient等轨级接口存在，Clip外检需要全链合同 | 在Clip effect schema引用稳定源/tap ID，graph编译请求该时间窗的detector并按B02对齐；预览、实时、导出同源。缺源显式诊断/交付阻断或用户明确改内部检源；不能预览自动回内部而导出另一种行为 |
| E03 卷积资源 | worker/四路径/取消已有；可移植IR asset与重连仍不足 | 将IR导入项目资源，保存fingerprint、来源rate/layout、trim/reverse/normalize参数与算法版本；worker生成候选kernel，ready后command关联。移动项目/同路径换内容/坏IR/取消均覆盖；单位脉冲与处理后的IR逐样本对拍。不得将显示秒数当已生效trim |
| E04 修复候选审核 | DeClick/DeHum已能选区离线提交；缺候选审核/更丰富参数 | backend返回candidate IDs/范围/置信/理由和输入key；接受/拒绝集合渲染同一候选栈，一次提交。未选声道/区外逐样本一致；取消不修改工程。批量错误候选可单次撤销 |
| E05 Pitch/Doubler进阶 | 基础离线F0/音阶/选区/retune已有；图形锚点、调式/实时版仍缺 | source-sample anchor含stable ID/音符/cent偏置，变速/剪裁映射复用B08；实时版声明延迟并接PDC，静音/无声/噪声直通，formant保持须单独算法/听测。443→440测试不能代表所有说话声自然 |
| E06 调制/速度图 | 手动BPM/拍值/部分波形已接；项目tempo map、retrigger和stereo相位合同待补 | 使用版本化`beat_at_sample`有理/分段积分，seek由绝对时间推相位；自由LFO和项目同步分别声明。BPM变化/循环/seek/分块输出一致，旧工程手动BPM不被自动替换 |
| E07 线性相位/高级拓扑 | Dynamic EQ线性相位、Thru-zero、更多电路/噪声塑形等无统一已验收链 | 每个新增algorithm独立ID/version；FIR群延迟接B02，bypass保持时序；非线性模型测alias/headroom，dither只在最终整数输出。没有需求时保留低优先级，不伪装经典硬件认证 |
| E08 LUFS Match/RT60/Freeze事务 | 有测量/Freeze基础，但异步分析/捕获事务和全端整合仍有缺口 | snapshot→worker→candidate→一次command；分析和应用共用revision/输入key；Freeze资源持久化并校验。LUFS match必须B04/B05回读实际结果，RT60用独立IR实例不扰生产DSP |
| E09 共同产品验收 | 合成测试不能证明对白/音乐可用性 | 所有生产实例跑参数往返、旧v0–v4/当前版本、重复set_params、旁路/seek/tail、随机块长、两实例独立、Clip/Track/Bus/Master能力表；真实对白、Foley、音乐等响A/B并记录设备 |

实施顺序：先E01/E02/E03（可信工程/声音链），再E04/E08（修复与测量闭环），最后按需求做E05–E07。每项先追踪字段“控件→Field映射→模型→Params→DSP消费”，列出实际消费键；只保存但DSP不读的字段判未实现。

算法要点：频谱以固定绝对hop聚合，FFT不在UI；IR反转/裁切应先生成候选IR再做卷积。Clip外检时间窗必须用source/clip/timeline映射，不能用Clip本地sample0直接读另一轨的sample0。动态时序状态保留，不能每次旋钮提交重新创建envelope。最终量化TPDF与noise shaping以持续跨块状态运行，不能每个块重置seed。

完成清单：每款效果器capability与实际一致；没有未消费参数被宣称生效；资源可移植；B01/B02共同合同通过；人工听音另有记录。原详细手册中的已完成算法保留，不为追求“全实现”破坏旧版本声音。

## B24：对白修复、环境匹配与可懂度工作流

优先级P1/P2；依赖B01/B04/B08/B13，实时适配另依赖B09。基础能力改进预计12–25人日；ML语音分离、实时de-reverb、客观可懂度工具分别单列模型/许可/性能评估，不能承诺一个短算法达到商业修复质量。

**现状证据**：[audio_denoise](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_denoise.py)、[audio_repair](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_repair.py)、[audio_edit](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_edit.py)已有降噪/频谱/点击与嗡声处理；B13已有room tone/对齐基础。完整的非平稳噪声跟踪、对白音色/环境匹配、经验证去混响/语音分离和可懂度报告尚不能从这些函数推定存在。

实施步骤：

1. 固定现有降噪算法和旧版本fixture；建立已知clean speech替身+噪声+IR的合成对，另用有授权真人对白作主观验证。目标是保护辅音/齿音/瞬态，不能只测噪声RMS下降。
2. 新`RestorationProfile`包含source hash、rate/layout、noise-only reference ranges、algorithm/version、reduction floor、attack/release、preserve_transients、选区/声道。监听residue不写工程，应用写现有audio_edit步骤。
3. 非平稳降噪先在可信非语音窗更新noise PSD（EMA或分位数）；语音/瞬态保护窗冻结或放慢更新。频率/时间平滑限制musical noise，幅度floor避免硬门。噪声profile不应误吸收台词或背景音乐。
4. 对白匹配分开响度、音色、环境三个分析：响度按B05；音色比较有声帧的平滑log谱差，限制每带提升/衰减（例如首版±6dB），输出少量可编辑EQ段；环境匹配优先使用B13真实room tone与B23项目IR。不要把整段PSD差直接当精确人声EQ。
5. 去混响/语音分离单列离线处理器，明确输入条件（单/多麦、前期噪声、声道链接）。可评估WPE延迟线性预测或经许可ML模型；先有中性/有声保护/算法版本/资源hash，再开放生产。WPE的预测延迟应保护直接声，矩阵解有正则化/病态拒绝，不能拿普通noise gate称作去混响。
6. 可懂度测量先明确有没有clean reference；有参考可评估相应客观指标，无参考只能标估计/代理，不能把响度、SNR或转录准确度直接称为标准可懂度认证。模型下载、版本、许可、语言覆盖与本机离线行为有记录。
7. 所有候选分析在worker，source/selection/generation改变即失效；处理前后与residue试听同一映射，选中声道外与区间外不变，取消/失败工程零变化。
8. 输出修复报告列过度衰减风险、未处理区间/无置信度原因。批处理只对明确profile的片段执行，一次意图可撤销；不自动改整部电影对白。

算法教学例：下面只演示从输入PSD和已知噪声PSD求一个受限Wiener gain，不包含噪声估计、STFT重构和去混响。可直接运行验证，不能作为完成本任务的全部实现：

```python
import numpy as np

def bounded_wiener(power, noise_power, reduction_db=12.0):
    power = np.asarray(power, dtype=np.float64)
    noise = np.asarray(noise_power, dtype=np.float64)
    if power.shape != noise.shape or np.any(power < 0) or np.any(noise < 0):
        raise ValueError('PSD shape/value invalid')
    if not np.isfinite(power).all() or not np.isfinite(noise).all():
        raise ValueError('PSD must be finite')
    if not np.isfinite(reduction_db) or reduction_db < 0:
        raise ValueError('invalid reduction limit')
    floor = 10 ** (-reduction_db / 20)
    return np.clip(1.0 - noise / np.maximum(power, 1e-12), floor, 1.0)

g = bounded_wiener(np.array([1.0, 1.0]), np.array([0.0, 1.0]))
assert np.allclose(g, [1.0, 10 ** (-12.0 / 20)])
```

状态版需要每bin独立的时间平滑：`alpha=exp(-hop/(tau_seconds*sample_rate))`；按相邻hop连续更新，不按process调用重置。重构用analysis/synthesis window和重叠归一化，未选区保留原PCM。训练/测试噪声不能用同一noise-only窗制造虚假泛化结果。

验证：固定不同SNR、50/60Hz、风噪、空调、混响IR、反相stereo与单边对白，测试clean输入中性、噪声改善、辅音保护、有限数值、选区/声道隔离和块长一致。±6dB匹配限幅必须被实际消费；匹配EQ用独立频响求值，响度与B05同测量口径。主观等响盲听至少对白/ADR/环境三类样本，记录是否有水声/金属感/音节损坏；量化门槛按算法选型建立，不凭一个SNR提升宣布修复质量达标。

失败/迁移/完成：旧步骤保留旧声音，新算法显式升级；缺模型/许可/资源/置信度时不产生伪成功。完成需要baseline、模型/命令/缓存/交付、residue试听、客观和真人对白验收，ML或de-reverb未做则独立标待办。

## 18. 跨任务测试配方与发布门

| 组 | 具体输入/操作 | 通过条件 |
| --- | --- | --- |
| G1 时间与坐标 | 23.976/24/25/29.97，44.1/48/96k，非零入点、倒放、原生变速、采样淡化 | 全链映射明确，边界≤1输出样本，长时不漂移 |
| G2 复杂图 | 直达+串联/并联Bus、pre/post Send、sidechain、PDC、tail | 共同目标时刻脉冲汇合，跨块一致，环/坏目标阻断 |
| G3 编辑与历史 | Properties/Clip Edit/时间线增删效果/自动化/Trim/VCA、锁轨、save/reopen | 一次意图一步undo；三视图同模型；实时/成片/Stem相符 |
| G4 交付 | 实际WAV/BWF/RF64/成片容器回读、FullMix和分类stem | layout/TC/count/hash完整；QC缺数据必阻断；按tap比较 |
| G5 生命周期 | A慢B快、缓存失败、取消、close、换工程、设备拔插 | 旧回包不写新UI；无线程/句柄泄漏；工程不丢 |
| G6 专业现场 | 双输入录音、punch、ADR cue、loopback校准、speaker patch | 样本时钟/监录分离正确；真实听音/设备报告齐全 |
| G7 外部互换 | 两套外部工具导出与导入AAF、真实5.1/7.1播放器 | handle/TC/增益/淡化/通道身份与已声明子集相符 |
| G8 长片 | 64轨90分钟、60分钟持续播放并编辑 | RSS/CPU p99/underrun/UI p95有记录，按机器预算通过 |

运行建议：先按任务跑focused tests，再跑`tests/mixer`及相关audio graph/automation/recording/stem模块。不要反复跑无关全仓巨型套件；发现新失败先判断是否基线问题，并记录准确范围。

任务状态模板（复制到后续实施记录，不在未做时勾选）：

```markdown
### Bxx / 子任务名
- [ ] 模型、单位和依赖审查
- [ ] 最小失败回归
- [ ] command + undo/redo + migration
- [ ] 实时/成片/Stem/缓存对拍
- [ ] 失败、取消、关闭、旧工程
- [ ] 实际UI入口验收（如适用）
- [ ] 设备/接收端验收或明确待测
- [ ] Review通过与证据记录
命令：...
结果：... passed；硬件/版本：...
剩余边界：...
```

## 19. 本轮执行记录

已执行只读核心审计组：monitor、VCA、Reconform、delivery preset、dialogue tools、PDC/stem sum、surround bus、audio graph snapshot，85项通过；与此同时审计脚本复现§7–10列出的组合缺口，说明既有测试覆盖仍不足。

算法示例18项通过。这证明参考代码自洽，不表示生产后端已经修复。UI整组回归413项通过；收尾后的导航/Compact/Properties/首次同步组49项通过；实际路由弹窗与相关controller兼容回归88项通过（各组有重叠，不相加）。新增导航测试13项及一项生产路由端到端测试覆盖本次UI。

主计划包含B00–B24共25项后端任务、完整B10–B20手册，89个本地文件链接已验证存在；11段工作流示例以及修复/空间声像教学代码实际运行通过，独立算法测试18项通过。中文760px及200px混音台、中文路由总览已离屏渲染并目视复核。测试不包含新的物理听音、Windows硬件或第三方AAF/插件往返，相关验收均留在后端任务中。

本轮证据见[执行报告](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.md)。所有后端任务保持待实施；没有因为现有85项核心测试通过而把五项反例标为解决。

### 批次1（2026-11-22，分支 bugfix）：B00 基线 + B01/B02/B03/B04 核心反例修复

审计脚本五类反例已按“先失败回归、后修生产路径”固化为真实 pytest
（`tests/edit/test_b00_counterexamples.py`，31项），修复后全部通过；审计
脚本重跑五项 `contract_satisfied=true`（JSON 已随本批次更新，修复前证据
保留在 git 历史 37dd3acc 的 JSON）。命令：
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_b00_counterexamples.py tests/edit/test_mixer_pdc_alignment.py tests/edit/test_pdc_and_stem_sum.py tests/edit/test_clip_fx_pdc.py tests/edit/test_audio_graph_snapshot.py tests/edit/test_audio_signature_completeness.py tests/edit/test_audio_graph_parity.py tests/edit/test_audio_three_way_parity.py tests/edit/test_audio_export_parity.py tests/edit/test_vca_groups.py tests/edit/test_delivery_preset.py tests/edit/test_delivery_qc.py`（约300项通过）；另跑 `tests/edit -k "audio or pdc or …"` 大组（596项通过、1项基线预存失败 `test_audio_h01_version_boundaries.py::test_upgrade_undo_roundtrip`，与本批次无关——stash 验证过）。

- **B00（部分）**：新增 `tests/support/sound_fixtures.py`（固定 seed 波形
  生成器、有理帧时钟、16/24/32f WAV 往返、对拍/null 工具）、
  `tests/support/reference_project.py`（10分钟 DX/ADR/Foley/FX/MX + 3 Bus
  + pre/post send + trim/VCA/自动化 模型构建器）。五类反例独立成测试。
  剩余：10分钟工程保存/重开/导出闭环、旧版本小工程 golden、44.1/96k 全
  组合三端对拍。
- **B01（部分）**：`edit_page._build_audio_timeline_signature` 补 send
  tap/muted、automation_trim、VCA 组（顶层新增第7元）；快照
  `TrackNodeSnapshot.sends` 升 4 元组、新增 `automation_trim`、
  `ClipNodeSnapshot.audio_gain_db`、`AudioGraphSnapshot.vcas` 并入
  stable_hash。参数化反例（每字段只改一项）全绿。剩余：SoundSnapshot 显
  式投影接全部消费端（预渲染 key/离线输入/缓存键）、schema_version、
  worker 提交前解析、路由原子宏事务（§7 步骤8）。
- **B02（核心落地，部分）**：`audio_dsp.plan_edge_pdc` 共享规划器（Kahn
  拓扑 + 逐边补偿 + 60s 上限截断诊断）；离线
  `AudioGraphRenderer._recompute_pdc` 与实时
  `AudioMixer._recompute_pdc_locked` 改为同一边级计划；主路边延迟在路由
  点应用（`route_track_output`/`render_bus_stage`/mix_samples 阶段1/2），
  每条 send 独立边延迟线（pre/post tap 按 clip/链延迟区分）；成片
  （timeline_export）与 Stem（stem_export）接同一 seam；`pdc_total_samples`
  = 最迟路径真实总量。审计串联 Bus 反例恢复单脉冲@400、幅度2.0；并行
  Bus+send 反例同步对齐；块长 733/4001/2000 无关性、mute 块推进、seek
  清线既有回归全绿。剩余：旁路/换采样率/插件延迟变更的图重编译交叉过渡、
  delay ring 预分配（当前仍为增长式缓冲）、sidechain 检测边对齐。
- **B03（部分）**：离线 `_TrackNode` 携带 volume trim 并在
  `render_track_block` 消费（反例 0.5→0.25 恢复）；实时 mix_samples ramp
  分支乘 VCA（反例恢复 0.25）；edit_page 图重建补 trim 与 VCA 下发
  （`AudioPlaybackManager.set_vca_overrides` 新委托）。剩余：VcaGroup 正式
  command/undo、保存重开三端对拍扩展、Bus/Master 作用域能力声明。
- **B04（部分）**：`evaluate_delivery` 增加必测防线——preset 声明限值的
  指标（integrated_lufs/true_peak_dbtp/peak_dbfs）缺测/None/bool/字符串/
  NaN/±Inf 一律 blocking（`delivery.{stem}.measurement_missing/invalid`），
  比较仅在防线通过后进行。既有 delivery/loudness 回归全绿。剩余：
  `MeasurementResult` 结构与真实文件回读 worker、checksum/manifest 绑定
  （§10 步骤2–6）。

边界说明：本批次未改任何命令/模型 schema（trim/VCA 消费既有字段），
无需迁移；修 PDC 会改变旧有问题工程的声音时序（发布说明须注明），
线性图在旧两级近似已对齐的形态下输出不变（既有 parity 组未动）。

### 批次2（8c304e2a）：B05 交付 profile/测量/发布 + B07 trim 烘入与保边简化

- **B05（核心，部分）**：`delivery_profile.py`——`DeliveryProfile`
  （schema 版本/来源核实/测量模式/逐 stem 规则/未知字段往返）、旧
  preset 迁移（legacy 标记 + 旧目标原样保留）、`MeasurementResult`
  （五态；文件 hash/布局/率绑定）、`measurement_rejection` 验收链
  （缺测/NaN/bool/静音规则）、manifest v2（profile+sound hash+逐文件
  sha256）、`publish_delivery_atomic`（暂存→校验→原子改名，失败零
  污染）。tests/edit/test_delivery_profile.py 16 项。
- **B07（核心，部分）**：`CoalesceAutomationTrimCommand`（trim 烘入
  曲线/静态 volume，帧级求值 parity、溢出拒绝、单步 undo）；
  `simplify_samples` 受保护帧（hold/触摸边界不删）；
  `simplify_volume_db` 以生产线性重放为 oracle 的 0.1dB 误差界。
  剩余：B07 全部写入作用域扩展/录制状态机完善/能力声明。

### 批次3（98609ffa）：B09 健康遥测 + B21 渲染产物 + B10 控制室 + B08 金字塔

- **B09（核心，部分）**：`AudioHealthSnapshot`（unknown=None 语义、
  渲染 p50/p95/p99 窗口、ring 统计注入、PDC/轨数/声音身份）；
  mix_samples 耗时采集；mixer/manager 双层 API。剩余：编译 worker
  隔离、deadline miss 计数、UI 健康面板接线。
- **B21（核心，部分）**：`render_artifacts.py`——`RenderArtifact`
  （freeze/bounce/printmaster 三语义、声音身份+依赖签名+文件绑定）、
  `freeze_staleness` 失效链、`ArtifactRegistry`（原子 sidecar）、
  `PrintJobLock`（revision 锁；输入漂移中止不混版）。剩余：bounce
  落 asset 的完整 command、freeze 边界合同与缓存接线。
- **B10（核心，部分）**：`monitor_service.py`——`MonitorService`
  （PFL/AFL/SIP/program/input 选择、布局矩阵 mono、5ms 增益斜坡）
  与监听隔离不变量（Master/Stem 字节不变）；`EditTrack.solo_safe`
  字段 + `SetTrackSoloSafeCommand`（apply/undo/序列化，旧工程
  默认 False）。剩余：图编译 tap 接线、设备 patch、校准 profile。
- **B08（核心，部分）**：`waveform_pyramid.py`——多级 min/max/RMS
  金字塔（极值保留、内存预算截断、概览列保守包含）与
  `plan_decode_window`（ClipExtent/DecodedWindow 分离、字节预算）。
  剩余：300 秒上限逐处理器解除、tile 缓存落盘、滚动窗接全链。

### 批次4（84af840e）：B11 采集时钟/Punch + B13 对白候选 + B14 comp + B20 参考mix

- **B11（核心，部分）**：`capture_clock.py`——`CaptureStream`（严格
  设备样本记账/缺块重复标记）、`PunchRange`/`punch_slice`（手册
  oracle：块×256、[250,780) → arange(250,780)）、`ClockAnchor`
  （±100ppm fixture 校正 ≤1 参考样本；±1000ppm 拒绝拟合）、
  `CaptureJournal`（fsync 节流 JSONL；崩溃半行/缺段只报连续前缀）。
  剩余：真机 loopback、`RecordingSessionPlan` v2 接线、跨设备漂移。
- **B13（核心，部分）**：`dialogue_proposal.py`——`DialogueProposal`
  只读候选 + `validate_proposal`（revision/指纹/置信度门槛）、
  `ApplyDialogueProposalCommand`（对齐写既有偏移字段：右移延迟/
  左移源侧修剪；低置信度不自动应用；单步 undo）、`RoomToneRef`
  （PCM 不进工程）、`RecorderSourceIdentity`（unknown 保留）。
- **B14（核心，部分）**：`audio_comp.py`——`AudioComp/CompRegion`
  （有序不重叠）、`replace_region`（手册 oracle，左右残片源偏移
  推进；100 次随机替换不变式）、`compile_comp_plan`（逐 take 渲染
  窗、边界 crossfade 区段长限制、缺 take/源短显式 offline 不换音）。
  剩余：命令化提交/试听/flatten 到共享 renderer。
- **B20（核心，部分）**：`reference_mix.py`——`ReferenceMixRef`/
  `MixVersion`（冻结登记）、`ComparisonState`（仅监听会话）、
  `comparison_gain`（手册 oracle + 上限拒绝）、
  `align_reference_offset`（1001 样本延迟 ≤1 样本对齐）。剩余：
  A/B 输出时钟切换、监听响度匹配闭环（B05 测量口径）。

### 批次5（d5b53029）：B12 AAF 音频子集 + 批次6（10f40280）：B06 真值锁

- **B12（v1 核心，部分）**：pyaaf2 1.7.1 引入并锁定（LICENSE 核实为
  MIT，旧注释 BSD-3 已更正）；`adapters/aaf_adapter.py`（Composition
  /Sound 槽样本编辑率/SourceClip 源窗/间隙/PCM 描述符外部引用；导出
  临时目录原子发布；bake 策略显式拒绝）。本地往返 ≤1 样本、轨名/槽
  身份/间隙保持、非 Sound/FX 进报告。**capability 保持 available=False**：
  外部接收端（Pro Tools/Media Composer 等）往返验收未执行——本地自读
  自写不冒充兼容（tests/edit/test_aaf_audio_subset.py + 门测试锁定）。
- **B06（真值锁，全链待实施）**：`test_b06_capability_truth.py`——
  capability 声明与渲染实际对锁（track_dsp/bus_master 立体声域、
  clip 阶段降混/轨链不吞声道分工）、BS.775 系数逐项、VBAP 手册
  oracle（±30° 对 0° → [√.5,√.5]；5.1 0° 精确命中 C；无高度布局
  仰角请求显式标记）。

### 批次7（本提交）：B15/B16/B17/B18/B19 手册 oracle + B22 稳定性回归

- **B15（核心，部分）**：`reconform_transaction.py`——`KeptSpan`/
  `remap_sample`（手册 oracle：半开区间、删除不吸附、歧义报错）、
  `plan_kept_spans`（帧域→样本域绝对边界，23.976 整数边界锁定）、
  `validate_reconform_transaction`（保护区间切碎/删除重叠拒绝整个
  提交）。剩余：完整事务命令与 cue/automation 联动、真实改版试片。
- **B16（核心，部分）**：`pro_audio_oracles.py`——`accept_block_reply`
  三元身份拒绝矩阵、`QuarantineRegistry`（指纹隔离/解除/容量）、
  `PluginDescriptor`。宿主进程/IPC/真实插件保持待实施（ADR 未签）。
- **B17（核心，部分）**：`MtcAssembler`（错序/重复不发布；完整序列
  组装）、`should_apply` 反馈去环（回声/时间窗/容量）、
  `SyncStateMachine`（acquiring→locked→freewheel→lost→重新捕获）。
  真机台/独立同步源保持待验收。
- **B18（核心，部分）**：`validate_track_uids`（手册 oracle：UID 形状
  /唯一/index 范围；numUIDs>numTracks 合法语义）+ 最小 chna 打包/
  解析往返（EBU 3285-s7 布局）。完整 ADM renderer/交付保持待实施。
- **B19（核心，部分）**：`estimate_roundtrip_samples`（233 样本手册
  oracle）、`bandlimited_probe`（带限低电平；极性反转可检）、
  `validate_patch_claims`（输出通道独占冲突）。真机 4 输出/硬件
  bypass 保持待验收。
- **B22（CI 部分）**：`test_b22_stability.py`——30× 模型序列化往返
  sound hash 不变（B00 参考工程，含 trim/VCA/sends 全字段）、未知
  payload 保留、100× undo/redo 回原始、coalesce 帧级求值 parity。
  夜间长压/故障注入/真机矩阵保持待实施。
- **B23/B24 现状核查**：`test_audio_fx_completed_controls.py` +
  `test_audio_denoise.py` + `test_audio_repair.py` + 
  `test_de_click_dehum.py` 33 项通过——已交付能力健康；E01–E09 与
  B24 进阶项按原计划保持待实施（不为"全实现"重写旧算法）。

预存基线失败（非本工作引入，已用 stash 对拍核实）：
`test_audio_h01_version_boundaries.py::test_upgrade_undo_roundtrip`、
`test_effect_registry_audio.py`（exciter/subharmonic ×4）、
`test_channel_layout_truth.py::test_unknown_layout_rejected_not_truncated`；
`test_timeline_envelope_editing.py::test_hit_performance_with_1200_nodes`
为机器敏感的边界性能测试（带/不带本工作均间歇超 250ms）。

### 批次8（2026-11-22 第二轮回扫）：整体隐藏缺陷审查与修复

全量交付后做的第二轮系统回扫（4 个并行审查代理覆盖全部新模块 +
本人复核 PDC 渲染/播放/导出三条缝），发现并修复（回归锁定在
`tests/edit/test_hidden_bug_sweep.py`，25 项）：

- **B15**：`validate_reconform_transaction` 保护区间判定重写为
  "安全 ⇔ 某个 span 完整包含"——旧判定对"span 含保护区间"误报
  split（拒绝合法事务），对"保护区间只被部分覆盖"漏报（保护内容
  落删除区仍放行）。
- **B02/PDC**：导出循环（timeline_export/stem_export）间隙零喂预算
  现为 **链尾音 + 最大出边延迟**（`renderer.pdc_out_edges_max_delay`
  公开访问器）——延迟线持留内容按绝对时间流出，不再在下一 clip
  起点错位涌出/导出尾截断；timeline_export 内联路由补齐 AUD-15402
  兜底（未知 bus → master 告警，不再静默丢整轨/send 建孤儿缓冲）。
- **B10**：`MonitorService` 5ms 增益斜坡按全局样本位置推进（块长
  < 斜坡长时旧实现把整段斜坡压缩进首块——64 样本设备缓冲实测 1.3ms
  到位）；混合长度 tap 输出取最大宽度（长 tap 尾部不再截断）。
- **B08**：`plan_decode_window` 视窗完全越过 extent 时吸附到末样本
  （不再越界 1 样本读解）；`range_extremes` 无整瓦片路径去除同段
  双重聚合（幂等性不再依赖 min/max 语义）。
- **B21**：`ArtifactRegistry._save` 写前重读磁盘按 id 合并——跨进程/
  多窗口写者不再互相覆盖删除。
- **B20**：`align_reference_offset` 拒绝 max_offset_samples<1 退化窗
  （corr[-0:] 整数组重复会让带外峰解码成大数值"高置信"错位）。
- **B11**：`ClockAnchor.timeline_sample_for_device` 无拟合缓存时按
  **最新锚点 + 理想斜率** 换算（单锚/拟合被拒后不再退回过原点恒等，
  4000 样本级错位）；`CaptureJournal` 按 sequence 从最新会话匹配 +
  恢复前缀在 sequence 重置处切换到最新会话（日志复用不再吞新 take）。
- **B17**：`MtcAssembler` QF6/QF7 解析按 SMPTE 规范修正（fps 位在
  QF7 bit2-3、时高位在 QF7 bit0-1；旧实现 fps 码永远读 0、25fps 时
  hours 被污染成 65）。测试编码器同步修正并补 25fps 流断言。
- **B18**：chna 记录布局按 EBU Tech 3285-s7 修正（UID ASCII 在记录
  偏移 2..13，非 4..15；旧布局与真实 ADM/BWF 工具不互操作）。
- **B12**：AAF **帧/样本域错误**（关键）——导出侧帧域 (帧, 子帧偏移)
  经 `frames_to_samples` 精确换算后写样本编辑率槽（旧实现把帧数值
  直接当样本，24fps×48k 下 2000 倍位移）；导入侧样本域 → (帧,
  子帧偏移) 二元组重述（F3.3），时长向上取整到帧并记诊断；裸
  SourceClip/Filler 槽（无 Sequence 包装）不再静默导入成空轨；
  尾部 Filler 长度按样本域比较；重叠 clip 顺序化诊断如实记录；
  失败路径清理 .tmp；未实现的 AafOptions 值显式 ValueError（不静默
  忽略）；Bus 轨进 DROPPED 报告。往返测试改按样本域比对（旧测试
  域混淆自洽、永不失败）。
- **B07**：`CoalesceAutomationTrimCommand` 烘入用 `dataclasses.replace`
  只改 value——贝塞尔手柄/tangent_mode/插值类型/pre-post 外推策略
  保留（旧实现在 BEZIER+linear 外推下 parity |Δ|≈0.36，违反 1e-12
  声音不变合同）。
- **B01**：快照 stable_hash 超集补齐——panner、audio:* 转场、master
  条带（EQ/FX）、clip 采样域淡化/子帧偏移/is_enabled 此前缺席
  （edit_page 签名有而快照无，原子图交换路径会 stale）；Bus
  `bus_layout` 补进**双签名**且图重建路径补 `set_bus_layouts` 下发
  （改布局/重开工程后引擎不再沿用旧布局混音）。
- **B05/B04**：`MeasurementResult.from_dict` 忠实保留测量值类型
  （float() 洗涤会让 True→1.0 绕过 bool 防线）；荒谬有限值
  （|v|>1e6）拒绝；`publish_delivery_atomic` 失败回滚本次新发布文件
  （零新增污染）+ 同名 basename 冲突显式 ValueError（不再静默覆盖
  前一 stem 内容）；`from_preset` 容忍 target_lufs=None（无 LU 目标
  语义）；`evaluate_delivery` None 限值不参与比较（不再 KeyError）。
- **B13**：`validate_proposal` 拒绝同一 clip 的多条 align（apply 端
  dict 折叠静默丢前一条）；`simplify_volume_db` 接入
  `AutomationRecorder.stop/stop_for_preview`（§13 步骤 8 的 dB 界
  此前只存在于测试，生产录制收尾仍走线性阈值）。
- 杂项：dialogue_proposal `List` 导入；capability_registry AAF 条目
  许可注更正为 pyaaf2 1.7.1 MIT（旧 BSD-3 残留）。

复核确认无需改动：`plan_edge_pdc` 算法本体（Kahn/环剔除/钳位）、
实时 mix_samples 各跳过点的延迟线推进、punch/comp/replace_region
数学、VBAP/chna 验证语义、sound_fixtures 往返。

批次8 全量验证（修复提交 ff5c86fe 后）：

- 全量 `tests/`：**45923 通过 / 292 跳过 / 111 失败 + 21 错误**
  （1:13:04）。全部失败经合并基点（65e33d9b）同检出对拍核实：
  **零新增**——均为预存基线（effect_registry ×4、wd13g69、
  h01、channel_layout 等）、原生编解码 SDK 环境态（16bit
  roundtrip/AAC/raw ×11，基点同败）或顺序敏感抖动（vault
  watcher/clip affordance gestures 等，隔离重跑两侧均过）。
- 已知非代码问题：`test_constant_color_picker_stores_scene_
  linear_values` 在长程全量中会因先前测试污染在 offscreen 取色
  器事件循环上永久阻塞（隔离 2.85s 通过、文件隔离 26/26 通过；
  faulthandler 5min 转储定位）。本轮验证以 `--deselect` 规避，
  根因（跨测试污染）属 color/UI 域，留待该域处理。

## 20. B10–B20完整实施手册

以下为主计划的组成部分。工时与依赖以§4表及各项现状说明为准。

## 如何使用这份手册

按依赖顺序一次完成一项。先运行相关现有测试，再写最小失败测试，随后实现模型、纯函数和命令，最后接已有 controller；不要从一个能点的按钮反推功能已完成。所有工程变更经过现有不可变模型及 command stack；临时监听状态不写进交付图。新接口名称均是建议新增，列出的“现有入口”是此次检查已确认的代码。

下面工作量是单名熟悉 Python/Qt/音频基础的开发者的**人日范围**，含定向测试与一轮审查，不含等待设备、购买 SDK、取得客户样片或外部软件许可的时间。新手应另留学习和返工时间。数字验收门槛是本项目建议的工程门槛，不是厂商认证或通用交付标准。响度、校准声压、声道顺序、同步容差必须绑定实际客户来源和版本，不从本文猜定。

共同提交清单：

- [ ] 模型/命令/服务与生产入口共同完成；undo/redo、保存重开、取消、锁轨、工程切换有覆盖。
- [ ] 音频作业只用不可变快照；发布检查 project/timeline/revision/generation；旧请求不能写回新工程。
- [ ] 非法输入返回带定位信息的诊断；缺后端、离线设备和未知效果不产生“成功”结果。
- [ ] 新 schema 有 version、显式迁移和未知字段保留；工程资源使用 asset/resource URI，设备 ID 只作本机设置的可重连提示。
- [ ] 定向 pytest 通过，再走本节人工验收；报告附版本、fixture、采样率、设备/驱动、结果和剩余边界。

从仓库根目录执行测试：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_monitor_section.py tests/edit/test_recording_manager.py tests/edit/test_dialogue_tools.py tests/edit/test_reconform.py --tb=short
```

本文中的 Python 示例只依赖标准库或项目已有 NumPy。可将单个代码块存入临时 `.py` 用 `.venv/bin/python` 运行。标注“教学参考”的部分必须扩成有状态、可取消、带能力检查的生产服务，不能直接塞进设备音频回调。

## B10　统一控制室监听、PFL/AFL 与 Solo-safe

**现状与依赖。** [monitor_section.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/monitor_section.py) 的 `MonitorState/MonitorSection.process` 已有输入监听音量、Mono、DIM、Mute；`pfl/afl/solo_safe` 目前只是字段，`process` 没有选择这些 tap。[recording_manager.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/recording_manager.py) 的 `_receive_pcm` 对录音前输入块应用这层监听；[adr_take_sheet_panel.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/ui/adr_take_sheet_panel.py) 的 `InputMonitorSink` 使用独立默认输出，并非统一节目控制室。依赖 B01 声音快照、B02 路径补偿、B09 稳定输出。工作量 12–20 人日。

**数据与接口。** 保留 `MonitorState`，新增本机 `MonitorSessionState(version=1, source_tap, gain_db, dim_db, mono_matrix_id, solo_mode, selected_listen_ids, output_patch_id, reference_profile_id)`。`source_tap` 只能指向图编译器公布的 `GraphTapRef(node_id, stage, layout)`。监听 profile 存本机设置，引用校准测量记录而非臆定声压。每轨 `solo_safe` 属声音模型，新增 `SetTrackSoloSafeCommand`；试听选择、DIM 和控制室音量用 `MonitorService.set_state`，不占项目 undo。新增 `MonitorService.render_block(program_taps, input_taps, state)`，位于最终设备路由前，禁止复用 Master gain 来实现监听音量。

**实施步骤。**

1. 绘制现有 program、Clip audition、ADR input 三条输出链，在服务层给设备访问加统一 owner。复用 [audio_arbiter.py](/Users/heng/Documents/vscode/topos-studio/src/shared/playback/audio_arbiter.py) 的会话互斥规则，明确哪些应互斥、哪些在同一图求和。
2. 明确 PFL 在轨道推子前、AFL 在轨道推子后；SIP 才修改节目可听集合。监听 solo 不能借现有轨道 `is_solo` 偷改导出声音。对每个 tap 写 sample-clock/latency 合同。
3. 单独实现 listen closure：PFL/AFL 选择监听 tap，是否叠加节目或输入监听由明确监听模式决定；SIP 复用路由可听闭包并加入真正的 solo-safe 返回轨规则。不要把 SIP 的 safe 规则直接套进 PFL/AFL。图编译拒绝不存在的 tap。
4. Mono 通过布局指定矩阵，不能对 5.1 简单 `mean(axis=0)`，否则 Center/LFE 语义错误。矩阵版本跟 B06 一致。DIM/Mute 以短增益斜坡切换，初始建议 5 ms，可配置并听测。
5. 将 ADR input sink 转成统一图 input tap；开关 input monitor 与 record arm 分离。监听开启失败明确提示，并保持录音源 PCM 不变。
6. 导出服务只接声音图，完全不接 `MonitorSessionState`。切页/关 Dock 不切断在用的节目图；关工程则停止/释放监听会话。

**算法参考：监听隔离的不变量。** 下面只演示 stereo AFL 与 DIM，不是完整 PFL 图。

```python
import numpy as np

def monitor_only(program: np.ndarray, *, dim: bool, muted: bool) -> np.ndarray:
    gain = 0.0 if muted else 10.0 ** ((-20.0 if dim else 0.0) / 20.0)
    return program * np.float32(gain)

master = np.full((2, 480), 0.2, np.float32)
before = master.copy()
speaker = monitor_only(master, dim=True, muted=False)
assert np.array_equal(master, before)       # Master/Stem 不能被监听改写
assert np.allclose(speaker, 0.02)
```

**验证。** 扩展 `tests/edit/test_monitor_section.py`，建议新建 `tests/edit/test_monitor_graph.py`。fixture 用 DX=997 Hz、MX=1511 Hz、Bus=独立脉冲；逐一选择 PFL/AFL/SIP，频谱核对信号身份。mute/mono/DIM/listen 开关前后的 Master 与 Stem 字节或浮点样本必须相同；5 ms 斜坡应严格符合生成的包络，零输入不产生非零输出。safe 轨在其他轨 solo 后仍可听，安全规则不能绕过锁轨。真实设备至少测内置与 USB 输出、耳机返回、输入回放与节目叠加；用低电平测试信号核对 patch，再做听音，记录实际延迟与爆音情况。

**失败、取消、迁移与完成。** 设备消失则输出静音并显示具体 patch，无自动切到未校准的扬声器；重新连接要求重验布局。旧工程默认 `solo_safe=False`，旧监听设置迁为本机默认 profile；不改原工程 Master 电平。完成需要：监听与录音/交付零副作用测试、tap/PDC 测试、双平台设备报告、会话释放测试全部通过。

## B11　录音时钟、Punch、多声道采集和中断恢复

**现状与依赖。** [mixer_recording_controller.py](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/mixer_recording_controller.py) 已支持输入 patch、多个轨 arm、共享输入及时间线录音；`_InputPatch.from_dict` 当前只接一/两个通道。[shared_recording_input.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/shared_recording_input.py) 可复用单设备共享采集。[recording_manager.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/recording_manager.py) 已有 `_ChunkedPcmBuffer` 磁盘溢写、失败保留 take、`measure_round_trip_latency`、`first_sample_timeline_frame`、`write_take_blocks`，不是从零开发录音。ADR cue 预卷/后卷/循环已有 [AdrRecordingControllerMixin](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/adr_recording_controller.py)。依赖 B01、B09、B10；多通道监听依赖 B06。工作量 20–35 人日。

**数据与接口。** 新增 `CaptureBlock(device_session_id, sequence_no, first_device_sample, host_monotonic_ns, pcm, discontinuity)`；由采集 backend 保证帧顺序。新增 `PunchRange(start_reference_sample, end_reference_sample)` 半开区间，不能用播放帧取整裁切录音。`RecordingSessionPlan(version=2, source_patch_ids, reference_rate, timeline_anchor_sample, pre/post_roll, punch_ranges, calibration_id)` 是工程/会话逻辑；机器设备与 buffer 配置另存本机。使用 `RecordingTransport.arm/start/punch/stop/cancel`；完成 take 后复用现有素材导入事务，新增 `CommitRecordedTakesCommand` 只在项目资产与时间线一起可提交时执行。

**实施步骤。**

1. 给现有共享输入加严格累计设备样本索引，识别重复/缺块。回调只入有界队列，文件写入与元数据生成走 worker；队列满不能悄悄丢样本继续宣称完整 take。
2. 录音起点用设备 sample clock 对 timeline rational clock 建锚；持续输入不以 `time.time()` 驱动定位。系统单调时钟用于观测，不替代音频时钟。
3. 先支持同一物理设备双工，测可重复 loopback 延迟并保存设备/采样率/buffer 组合。跨设备必须测漂移，维护相位连续的异步重采样；不支持时明确拒绝同步录音，而非只补一个固定 ms。
4. 连续采集预留 handles，Punch 只定义有效使用窗；进入/退出边界按整数样本切片。后台落盘临时 take 不立即破坏原 clip；停止后用一条事务插入，原录音与所有 punch handles 可追溯。
5. `_InputPatch` 升到 v2：显式任意合法 `channels` 列表和通道标签；不要用 2ch 向量误处理 Poly WAV。共享采集一次读取 N 通道，每目标轨只做 index map。
6. 每若干块更新恢复 journal，记录已 fsync 的段、sample count、CRC/指纹与会话来源。磁盘满、掉线、程序重启可将连续有效前缀恢复成 partial take，断点保留诊断。完成文件写到临时路径，校验后原子改名。
7. 让校准点击、ADR beep、节目回放共享时间锚；录音时切页/关闭工程先停止并等待必要 flush，通过 session generation 防止旧 take 导入别的工程。

**算法参考：Punch 的块边界切片。**

```python
import numpy as np

def punch_slice(block: np.ndarray, first: int, start: int, end: int) -> np.ndarray:
    if end <= start:
        raise ValueError("empty punch")
    lo, hi = max(first, start), min(first + block.shape[1], end)
    return block[:, max(0, lo - first):max(0, hi - first)] if hi > lo else block[:, :0]

blocks = [np.arange(i, i + 256)[None, :] for i in range(0, 1024, 256)]
recorded = np.concatenate([punch_slice(b, i * 256, 250, 780)
                           for i, b in enumerate(blocks)], axis=1)
assert np.array_equal(recorded[0], np.arange(250, 780))
```

漂移估计先用成对锚 `(device_sample, timeline_sample)` 最小二乘拟合斜率；例：设备走 480048 样本而 reference 走 480000 样本，则每输入样本消耗 reference 比例为 `480000/480048`。这只说明比例，不能把 `np.interp` 当生产异步重采样器；生产复用有状态重采样器，缓慢调节 ratio，限制 slew，断点重新建锚。

**验证。** 扩展现有 `test_recording_manager.py`、`test_recording_import_transaction.py`、`test_adr_recording_workflow.py`；新增 `test_capture_clock_punch.py`。fixture：8 通道依次单脉冲、257/733/1024 随机块，Punch `[250,780)`，实际写出530样本且通道互不串漏。模拟±100 ppm漂移是压力 fixture，不是硬件容许规范；连续30分钟虚拟运行，校正后锚误差≤1 reference sample、不重复/丢块。注入磁盘满/短写/中断，恢复文件样本数必须等于 journal已确认数。真机跑48/96k、1/2/8输入，测10次loopback分布和一小时录放偏差；手动拔USB、切默认设备、录音时关工程和重启，核验 partial BWF可读、既有take不覆写。

**迁移与完成。** v1输入patch升级v2时保留原通道顺序，缺设备保持unavailable；旧BWF元数据不改写。旧工程没有Punch配置即普通录音。完成需要：时钟/丢块/准确punch/恢复/真实设备证据；硬件未测的驱动组合仍标未验收。

## B12　AAF 交接子集与真实往返

**现状与依赖。** [interchange.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/interchange.py) 的 `INTERCHANGE_CAPABILITIES` 明确 AAF `available=False`；现有 FCPXML/OTIO 不能代替 AAF。[interchange_types.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/adapters/interchange_types.py) 定义 `InterchangeAdapter/ImportResult/InterchangeReport`，可复用其 preserved/converted/dropped/requires_user_action 四类报告。现有 adapter 在 [adapters 目录](/Users/heng/Documents/vscode/topos-studio/src/features/edit/adapters)。依赖 B01、B04、B05、B08，复杂效果 bake 依赖 B21。工作量：限定音频子集25–45人日；扩不同接收软件和复杂效果另估。

**格式边界和接口。** 第一版仅音频 Composition、轨道、源媒体引用/嵌入PCM、sample edit rate、片段裁剪、音量、线性淡化、handles、起始TC。先选择一组实际可交付的接收端版本，写入 profile。拟新增 `adapters/aaf_adapter.py`，实现既有协议；`inspect` 不能修改项目，`import_to_canonical` 只产候选。新增 `AafOptions(version=1, target_profile, media_mode, handle_samples, unsupported_policy, preserve_source_metadata)`。工程不存绝对机器媒体路径，出包在专用目录写相对资源表。

AAF 的 CompositionMob、MasterMob、SourceMob 有不同职责，应按对象规范处理源身份与编辑决定，不能只把一条 Master WAV封进AAF。参见 [AAF对象规范](https://aafassociation.org/html/specs/aafspec-v1.0.1.pdf)。候选库 [pyaaf2官方仓库](https://github.com/markreidvfx/pyaaf2) 已核查为 MIT；仓库旧注释称 BSD-3，实施时应核对锁定版本 LICENSE 并修正能力说明，不能沿用未核实许可。

**实施步骤。**

1. 固定依赖版本，保存许可证、最小导入/导出样例、接收端profile；安装是实施阶段工作，本轮未加依赖。
2. 建一张字段映射表：Topos `asset_id/clip_id/source sample/timeline sample/fps` 到 MobID/slot/edit rate/SourceClip.start/length。音频长度优先样本编辑率；画面TC轨用有理帧率。DF是编号规则，不改声音速度。
3. `inspect` 设置文件大小、对象数量和嵌套深度预算；拒绝路径穿越和循环引用。嵌入媒体抽取到临时资源区，用内容指纹去重，用户确认后才注册资产。
4. 导出按片段源窗加handles，越源界截断并报告；每段保留原source id、原窗与baked窗的对应。移交后有空间再次修剪，不能把handles算进可听时间线长度。
5. 淡化/自动化只转换接收端已验收的子集。未知效果、时间拉伸和复杂转场按 `bake/cancel` 明确决定；烘焙后报告原链快照与生成音频指纹。
6. 导入生成候选资产+时间线+报告，经现有项目事务一次提交；undo不能遗留孤儿注册资产。导出采用临时目录，AAF、音频、manifest全部成功后发布。
7. 为两套独立接收工具制作同一fixture并往返；同时核对接收端重建声音和可编辑信息，不能只用本库自读自写证明兼容。

**算法参考：有理编辑率换算。**

```python
from fractions import Fraction

def nearest_integer(value: Fraction) -> int:
    if value < 0:
        return -nearest_integer(-value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)

def convert_position(position: int, from_rate: Fraction, to_rate: Fraction) -> int:
    return nearest_integer(Fraction(position) * to_rate / from_rate)

assert convert_position(24000, Fraction(24000, 1001), Fraction(48000)) == 48048000
start, end = 101, 733
assert convert_position(end, Fraction(48000), Fraction(96000)) - \
       convert_position(start, Fraction(48000), Fraction(96000)) == 1264
```

生产中复用 [sample_time.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/sample_time.py) 的有理转换，分别转换绝对起止点后相减，不对每段时长逐段取整累加。

**验证。** 新增 `tests/edit/test_aaf_audio_subset.py` 与集成fixture。合成5轨、混合mono/stereo、3个不同源入点、12/24帧handles、48/96k、23.976/24/25/29.97DF项目；源PCM在每个秒边界放唯一脉冲。导入后源窗误差≤1样本（格式允许sample edit rate时），图像TC精确一帧不差；PCM未烘焙媒体指纹相同。模拟损坏Mob、离线media、同名异源、超大嵌入、取消、目标磁盘满。人工记录接收端名称/版本，核验轨名、TC、handles可trim、fades、gain、声道顺序、重建后声音与参考WAV残差；特定接收端只能帧级时必须在报告标出实际误差，不能标无损。

**迁移与完成。** 不改变旧工程AAF能力状态；只有依赖、生产菜单、支持矩阵、两套接收端报告和失败测试齐备才设available。未知AAF属性存原始扩展payload或明确报告不可保留；绝不悄悄丢掉。完成清单还包括取消不产生半包、一次undo回到导入前、异机资源重连。

## B13　对白工具闭环与 Field Recorder 匹配

**现状与依赖。** [dialogue_tools.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/dialogue_tools.py) 已有 `fill_room_tone/estimate_alignment_offset/plan_batch_crossfades/plan_jl_cut/plan_gap_splits`；[timeline_view.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/ui/timeline_view.py) 已接gap split和同场对白组，不能重写这些已交付算法。素材 [MediaAsset](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/project.py) 已有reel/scene/shot/take；[bwf.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/bwf.py) 能读bext。RoomTone、匹配与对齐仍缺完整候选→命令→生产交付链。依赖B01、B03、B08；多麦同步编辑与B14协作。工作量18–30人日。

**数据与接口。** `DialogueProposal(version=1, project_revision, source_fingerprints, operations, confidence, warnings)` 只读候选。`RoomToneRef(asset_id, source_start_sample, source_end_sample, channel_map, crossfade_samples)` 不存PCM到工程。`RecorderSourceIdentity` 扩展原素材metadata：BWF time reference、UMID（有才用）、iXML文件/场次镜次/通道名、实际source rate、录制日期；缺字段保持unknown。命令建议 `ApplyDialogueProposalCommand` 组合既有clip/audio commands；对齐写既有采样偏移字段，不能直接改UI播放头伪装对齐。新服务 `DialogueToolsService.propose_*` 后台计算，`validate(proposal, current_revision)` 再提交。

**实施步骤。**

1. RoomTone先选择真实底噪源窗；禁止自动从一段台词里剪“静音”当环境。复用fill函数，改成资源/步骤描述并走预渲染；固定seed和算法版本，使缓存、试听与交付一致。
2. 对齐先用元数据/BWF缩小搜索窗，再复用相关估计。长对白先分窗、去DC/带限、能量门控；输出偏移、峰值、次峰比和候选区间。相同周期音/静音低置信度不自动接受。不同表演ADR可只出建议，禁止宣称可用单个相关偏移完成口型匹配。
3. 匹配外录来源按稳定ID→卷号/日期/场次镜次→时间窗→通道名逐级过滤；文件名只能弱提示。多个候选并列时保留，选择后写source binding，不覆写原机内声资产。
4. 多麦克风作为源组，选择clip后可展开/切换麦克风；共享时基/剪点，增益与phase可独立。对齐应用整组偏移，禁止每声道独立拉伸破坏相位。
5. 批淡化先用plan函数找邻接边，再校验真实handles、重叠、已存在转场、锁轨和链接音画。一条宏命令提交全部有效pair，同时列跳过原因；不能一半提交失败。
6. RoomTone填补、J/L cut和对齐均先显示候选，复用已有试听/长度事务；适用范围/声道/输入输出坐标在proposal明确。提交前revision变化则重新计算。

**算法参考：同一录音的整数样本对齐 oracle。** 仅用于合成fixture和相关符号校验，不代替生产窗口化FFT或对白变形算法。

```python
import numpy as np

def delayed_take_offset(reference: np.ndarray, take: np.ndarray, limit: int) -> int:
    scores = []
    for lag in range(-limit, limit + 1):
        if lag >= 0:
            a, b = reference[:len(reference) - lag], take[lag:]
        else:
            a, b = reference[-lag:], take[:len(take) + lag]
        a, b = a - a.mean(), b - b.mean()
        denominator = np.linalg.norm(a) * np.linalg.norm(b)
        scores.append(float(a @ b / denominator) if denominator > 1e-12 else -1.0)
    return int(np.argmax(scores)) - limit

rng = np.random.default_rng(13)
ref = rng.normal(size=2048)
take = np.r_[np.zeros(37), ref[:-37]]
assert delayed_take_offset(ref, take, 100) == 37   # take要向前移动37样本
```

**验证。** 扩展`tests/edit/test_dialogue_tools.py`、`test_dialogue_edit_group.py`，新增`test_field_recorder_match.py`。fixture用同名不同UMID、相同场次不同take、BWF重叠窗、多麦one-hot脉冲、不同率和空元数据；唯一ID匹配正确率100%，歧义不自动绑。相关同源延迟±37/±239样本误差≤1样本；静音/纯音应拒绝自动对齐。RoomTone输出长度精确不变、非选声道逐样本不变、边界连续性用明确crossfade包络对拍；批淡化一次undo恢复所有clip。人工用领夹/吊杆、不同表演ADR和真实环境听loop接缝、梳状滤波、辅音完整性；结果记录成“人工确认”，不虚构自动算法阈值。

**迁移与完成。** metadata扩展需保留未知iXML，不把日期缺省为今天；旧binding继续有效。取消/源离线保留原clip和RoomTone source ref，坏signature不fallback成原声后报成功。完成包括生产候选入口、统一试听/成片/Stem、源可追溯、组同步和单步undo。

## B14　Playlist、Comp 与分句选 Take

**现状与依赖。** [audition.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audition.py) 已有 `Audition/AuditionTake/switch_take/flatten_audition`，支持候选整体切换和fit/ripple。[take_sheet.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/take_sheet.py) 管ADR录音take，不等于时间线逐句comp。现有整体候选与工程auditions字典必须复用；`hold_last`为既有fit策略，音频短take不应复制最后一个样本伪造持续音。依赖B01、B03、B11、B13。工作量18–30人日。

**数据与接口。** `AudioComp(version=1, comp_id, track_id, lane_ids, active_regions, revision)`；region包含`start_reference_sample/end_reference_sample/take_id/source_start_sample/fade_in/out`。样本区间半开、同一comp活动区不重叠；底层lane只引用已有AuditionTake/asset，保留完整原始录音。命令`AddTakeLaneCommand/SetCompRegionsCommand/FlattenAudioCompCommand`；服务`compile_comp(comp, source_resolver) -> clip/transition plan`，先落到既有clip/transition模型再让共享renderer消费，避免额外一套音频求和器。

**实施步骤。**

1. 扩展现有Audition模式区分whole_take与audio_comp；旧whole_take保持原语义。TakeSheet最佳take只是排序提示，不直接改活动comp。
2. 建有序不重叠区间索引；用户替换区段先切原区域两端，再插新take，并保留左右残片。未覆盖区域明确静音或指定底层lane，不能隐式拼别的take。
3. 对切换边界检查两侧源handles；默认短crossfade由源窗允许长度决定。立体声和成组麦克风共用剪点/样本时基；不同采样率先有理映射。
4. 在内存生成临时clip/transition plan试听；确认再一次命令提交。拖动跨多区域时只一条历史记录；Esc恢复原plan。
5. flatten时保留原comp描述与take资产，bake资源写入项目媒体管理；撤销恢复原comp。AAF导出按B12能力拆平并报告。
6. 清理未使用take必须单独可审核操作；不能因为当前region不引用它就删原文件。

**算法参考：替换活动区段，保留左右残片。** 此例region的源偏移用与时间线相同样本率；生产通过sample_time换算。

```python
from dataclasses import dataclass, replace

@dataclass(frozen=True)
class Region:
    start: int
    end: int
    take: str
    source: int

def replace_region(regions: list[Region], new: Region) -> list[Region]:
    if new.end <= new.start:
        raise ValueError("empty region")
    result = []
    for old in regions:
        if old.end <= new.start or old.start >= new.end:
            result.append(old)
            continue
        if old.start < new.start:
            result.append(replace(old, end=new.start))
        if old.end > new.end:
            result.append(replace(old, start=new.end,
                                  source=old.source + new.end - old.start))
    return sorted([*result, new], key=lambda r: r.start)

got = replace_region([Region(0, 1000, "A", 100)], Region(300, 600, "B", 20))
assert got == [Region(0, 300, "A", 100), Region(300, 600, "B", 20),
               Region(600, 1000, "A", 700)]
```

**验证。** 新建`tests/edit/test_audio_comp_regions.py`，继续跑现有audition/take测试。fixture三个take分别997/1511/2017Hz，编译region `[0,300)` A、`[300,600)`B、`[600,1000)`A，核对源偏移、样本数和fade oracle。随机100次区间替换后region仍有序/不重叠，undo逐步还原；缺take仅该区段offline而不偷偷换音。人工录三遍同台词，从不同take挑句，检查对齐、换麦组同步、呼吸/辅音和听感边界。

**迁移与完成。** 不自动将所有Audition升级comp。未来未知comp版本以只读方式保留payload并阻断不可信导出。取消不注册新媒体；关闭工程清理临时编译计划。完成包括区域选择、原take保留、render/AAF flatten、一次undo与重开一致。

## B15　Reconform 画面改版事务

**现状与依赖。** [reconform.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/reconform.py) 提供`shot_fingerprint/match_shots/plan_audio_reconform`，尚无完整生产执行事务。当前指纹主要由四段亮度均值/方差量化后哈希；`match_shots`用ID或指纹相等做LCS，未将`source_tc_frames`加入匹配，长列表O(nm)。重复镜头/黑场与调色重编码易造成歧义，不能把原型直接当自动改版服务。依赖B01、B08、B13、B14；复杂音乐尾音打印依赖B21。工作量25–45人日。

**数据与接口。** `ReconformPlan(version=1, old_revision/new_picture_id, source_fingerprints, fps_mapping, changes, conflict_groups, protected_tracks, decisions)`。每个change带旧/新半开样本窗、匹配理由/置信度和handles。`ReconformService.inspect/propose/validate/apply`；`ApplyReconformCommand`保存完整可逆变更集，不能只存最终clip列表，必须包含转场、automation、marker、ADR cue和同步组。

**实施步骤。**

1. 优先读取旧/新剪辑交换表的source ID/reel/source TC/duration，指纹作弱辅助。全黑/重复镜头、同ID不同源窗给冲突，不以第一个哈希命中自动通过。
2. 对确定锚分段匹配；长序列用稀疏候选/Hirschberg等节省内存，避免完整n×m表。变帧率/pull处理先定义有理时间映射，禁止float累加。
3. 将每个保留源窗映射到新版：剪点内clip可切分，越界部分按handles保留。音乐/保护轨不自动拆碎，列需要人工决定的冲突。
4. 参数automation、片段包络、marker/cue按同一分段映射移动；连续曲线在切点插边界值，不只移动已有节点。孤立/被删区段保留恢复引用或进入冲突列表。
5. 先产生只读候选与变更列表；用户接受若干组后，以revision检查一次事务落地。任何锁轨/源不足冲突应拒绝整个提交或明确限定范围，不能半途移动。
6. 原版序列保留不可变版本引用；可导出change list与旧/新picture校验。undo必须连同cue、automation和路由相关引用完整恢复。

**算法参考：分段保留映射。** 半开区间避免切点被映射两次；删除区段返回None，禁止吸附到“最近”位置。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class KeptSpan:
    old_start: int
    old_end: int
    new_start: int

def remap_sample(position: int, spans: tuple[KeptSpan, ...]) -> int | None:
    matches = [s for s in spans if s.old_start <= position < s.old_end]
    if len(matches) > 1:
        raise ValueError("ambiguous source mapping")
    return matches[0].new_start + position - matches[0].old_start if matches else None

spans = (KeptSpan(0, 100, 200), KeptSpan(150, 300, 0))
assert remap_sample(40, spans) == 240
assert remap_sample(100, spans) is None
assert remap_sample(180, spans) == 30
```

**验证。** 扩展`tests/edit/test_reconform.py`，新增`test_reconform_transaction.py`。fixture A/B/C/D重排、插黑/删镜/重复B、同reel不同TC、变帧率、长跨场音乐、cue/automation跨切点；保留区间内one-hot脉冲精确落新位置≤1样本。删除内容不能悄悄复活，ambiguous候选自动提交数=0。一次undo后`TimelineSequence/项目相关payload`与原版相同。用30分钟合成镜头列表压测候选构建与取消，再用实际新旧画面人工spot check头/中/尾、删场处、音乐连续与台词口型；须留时间码和人工签字结果。

**迁移与完成。** 旧工程无plan不迁移剪辑；plan引用过期源/版本时只读并重新计算。取消工作只删候选cache；失败不影响旧版。完成包括冲突审核、事务、版本保留、自动化/cue映射和真实改版验收。

## B16　第三方插件宿主：先隔离，再声音合同

**现状与依赖。** 内部效果通过 [effect_registry.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/effect_registry.py)、[audio_dsp.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_dsp.py) 的`EffectChain`运行；不存在已验收的VST3/CLAP/AU/AAX宿主。内部panel数量不能当第三方兼容度。依赖B01、B02、B06、B07、B09、B21；先完成立体声一个格式，再扩其他格式。工作量：一种格式的有限beta 40–80人日；跨平台/多格式再40–100人日，SDK/许可评估另计。

**数据与接口。** 插件身份`PluginDescriptor(format, vendor, stable_plugin_id, version, binary_fingerprint, supported_layouts)`；工程实例`HostedEffect(version=1, instance_id, descriptor_ref, parameter_ids, state_asset_uri, state_hash, bypass, missing_policy)`。二进制state走受控资源不塞巨大base64进json；绝不按显示名称重绑未知插件。宿主进程协议`scan/load/configure/set_state/process/get_state/dispose`；`process`输入block index、sample position、layout和参数事件sample offsets，输出PCM、latency、tail、diagnostic；control与audio传输分开。

**实施步骤。**

1. 制作ADR：选择首个SDK/格式/平台、许可、支持bus/event子集，基于官方最小host和测试插件验证，不自己猜插件ABI。VST3回调生命周期、bus重配和latency变化参考[Steinberg官方调用序列](https://github.com/steinbergmedia/vst3_dev_portal/blob/main/src/pages/Technical%2BDocumentation/Workflow%2BDiagrams/Audio%2BProcessor%2BCall%2BSequence.md)。
2. 扫描独立短命进程，超时/崩溃进入quarantine，以二进制fingerprint重新扫描。扫描不在主应用导入动态库，不自动给不可用插件打开入口。
3. 第一版每插件实例或小隔离组worker，控制IPC传描述，音频共享内存传预分配块；不对每块pickle NumPy。与B09合同定义在处理截止前完成，missed deadline产生明确runtime failure。
4. latency/tail和bus变化通过新图编译生效；旧运行实例仅在音频线程安全点释放。旁路必须延迟保持，不能让声音瞬移；可补偿与低延迟录音模式分别定义。
5. 参数自动化以稳定parameter ID与样本偏移提交，state restore后读回验证。UI窗线程与DSP线程独立，插件editor关闭不销毁正在发声实例。
6. 崩溃后保留最后可信state/日志，停止受影响链或按用户明确missing_policy播放；交付默认阻断，不能静默dry成功。可选恢复需新generation。
7. 离线导出使用同版本插件、相同state/seed/sample clock；不确定性插件标记并提供B21打印替代。升级插件前做state兼容回放，不就地重写旧state。

**算法参考：结果代际与顺序拒绝。** 这是控制层oracle，实际audio callback不可在Python里临时分配/阻塞等待。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class BlockReply:
    generation: int
    index: int
    frame_count: int

def accept_reply(reply: BlockReply, generation: int, next_index: int, count: int) -> bool:
    return (reply.generation, reply.index, reply.frame_count) == (generation, next_index, count)

assert accept_reply(BlockReply(3, 10, 256), 3, 10, 256)
assert not accept_reply(BlockReply(2, 10, 256), 3, 10, 256)
assert not accept_reply(BlockReply(3, 9, 256), 3, 10, 256)
```

**验证。** 新建`tests/edit/test_hosted_plugin_contract.py`与独立native测试。自制开源测试插件覆盖unity、gain、exact delay、tail、参数事件、crash、hang、bad layout、非法state；unity null残差≤1e-6，延迟脉冲按B02精确对齐，随机块长回放误差按插件算法预先声明，不同插件不可统一许诺bit exact。旧generation/重复block零接受，deadline失败有可见诊断。使用至少两家合法取得的插件人工测扫描、打开editor、保存重开、离线打印和缺插件重连；报告具体版本。厂商兼容测试、许可证/SDK要求不能用Python单测替代。

**迁移与完成。** 旧内部EffectInstance保持原type和算法版本；第三方使用独立稳定catalog条目，不冒名覆盖内置效果。未知/缺插件保留state和参数原文，导出前要求恢复或明确bake。取消导出释放worker、shared memory和临时state；完成需要host隔离、崩溃恢复、PDC/自动化/离线一致、合法SDK与真实插件报告。

## B17　控制台协议、MTC/LTC 和外部同步

**现状与依赖。** [shared/control_surfaces/tangent](/Users/heng/Documents/vscode/topos-studio/src/shared/control_surfaces/tangent) 是调色面板相关基础，不代表已经接HUI/Mackie/EUCON音频台。时间码转换已有 [interchange.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/interchange.py) 的`frames_to_timecode/timecode_to_frames`，有DF处理；这不是外部时钟锁定。依赖B01、B07、B09、B11。工作量：单控制协议12–20人日，MTC/LTC chase20–35人日；专有SDK另估。

**数据与接口。** 分开两条服务：`ControlSurfaceService`和`ExternalSyncService`。控制事件`SurfaceGesture(session_id, event_id, origin, control_id, phase, value, monotonic_ns)`，phase包括begin/update/end，接既有preview/commit/automation recorder。同步事件`TimecodeObservation(rate_num/den, drop_frame, position, direction, received_monotonic_ns, validity)`；`SyncState`为stopped/acquiring/locked/freewheel/lost。项目可存对齐offset/pull策略；端口名/设备ID放本机profile。

**实施步骤。**

1. 一种公开可验证的协议先行，以adapter输出归一化动作，不让协议字节直接修改EditTrack。begin/update/end分别接预览/采集/单次commit；bank切换不改变轨ID。
2. 应用到设备的反馈包含origin/last value/token，过滤echo，自动化回放驱动motor fader不再次写automation。硬件触摸优先于软件追随的规则明确。
3. MTC按[MIDI协会规范](https://midi.org/midi-time-code)处理Quarter Frame/Full消息，完整组装后才发布；错序、重复、倒放、跳码需要独立测试。LTC通过受控decoder接输入tap，不能把解码放UI paint。
4. 时间码只给位置参照，不天然提供共同音频采样时钟。优先选择明确主时钟；chase的频率校正、seek阈值、freewheel时间与失锁动作写profile，不能不断硬seek导致爆音。
5. offset/pull用有理数进行一次定义；例如客户规定的1000/1001速率修正同时影响duration与resampling，不能只改标签。用B11时钟映射保持累计样本相位。
6. 控制端断连结束所有touch gesture、释放latch临时状态；sync失锁进入可见freewheel/lost，录音是否继续由session policy明确，记录不连续区间。

**算法参考：反馈去环与动作身份。**

```python
def should_apply(origin: str, event_id: int, local_origin: str, seen: set[tuple[str, int]]) -> bool:
    key = (origin, event_id)
    if origin == local_origin or key in seen:
        return False
    seen.add(key)
    return True

seen: set[tuple[str, int]] = set()
assert should_apply("desk", 41, "app", seen)
assert not should_apply("desk", 41, "app", seen)
assert not should_apply("app", 42, "app", seen)
```

生产`seen`必须有容量/时间窗上限；只按value去重会误吞下一次合法重复触摸。完整chase的PLL不是这个例子覆盖的算法，应单独建立观测器测试，再接设备。

**验证。** 新建`test_control_surface_gestures.py/test_external_sync.py`。fixture回放正常/乱序/丢包协议序列，10万条反馈消息不能产生递归commit，单个触摸一次undo；MTC只有完整合法消息可更新position。模拟30分钟23.976/25/29.97DF、跳码/倒放/dropout，锁定后位置误差按客户profile阈值验收，不能擅设广播认证数字；rational换算自测≤1样本。真机测试一台motor fader台与一套独立timecode源，录制双端时间戳/音频脉冲；EUCON等专有协议须具备对应SDK/授权和实际设备才开放能力。

**迁移与完成。** 旧工程保持internal clock默认；无已配置端口时不自动连接别人的控制台。profile未知版本只读；断连取消gesture并保留已提交工程。完成需协议parse/echo/command测试、采样时钟与时间码关系说明、真实台/同步源证据。

## B18　ADM/BWF 与对象音频

**现状与依赖。** [layout_capability.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/layout_capability.py) 明确高度布局不支持，track/master仍立体声为主；[channel_layout.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/channel_layout.py) 目前enum不含height/object。已有BWF bext不是ADM的axml/chna对象描述。依赖B06原生多声道全链、B02、B04/B05、B07、B12基础交接；仅采用hosted插件renderer时依赖B16，独立库/独立进程renderer不天然依赖VST宿主。工作量首个明确定义ADM子集40–70人日；特定发行/认证profile另立项目。

**数据与接口。** `ImmersiveScene(version=1, programme, contents, objects, beds, channel_formats, track_uids, pack_formats, renderer_profile)`；Object使用稳定ID、引用源轨、sample-rate时基位置/增益曲线，明确坐标系/单位/插值。`BedRoute`保留每通道身份。新增`AdmService.inspect/import/export/validate`，`RenderImmersiveScene`接明确定义renderer。ADM是描述模型，不能把导出ADM文件等同某商业Atmos格式认证。术语、渲染器与工具入口参考[EBU ADM渲染背景](https://adm.ebu.io/background/rendering.html)、[EBU ADM工具清单](https://adm.ebu.io/tech/adm_software.html)。

**实施步骤。**

1. 固定目标ADM版本/profile、bed/object子集、输出渲染布局与renderer版本；用客户/规范来源记录限制。B06未完成时保持功能禁用，禁止stereo复制成7.1.4伪装完成。
2. 基于sample clock保存object automation，映射到ADM block格式时保留边界和插值语义；交付范围截取时需插入起点有效值，不能丢掉范围外此前的对象状态。
3. PCM轨序和`audioTrackUID/TrackFormat/PackFormat/ChannelFormat`建双向一致表；bed与object不能混淆。使用XML安全解析、ID唯一与引用完整校验。
4. WAV包装按目标profile写axml/chna等必需chunk，复用现有BWF读取/大文件处理；bext元数据与ADM引用共同校验。[EBU Tech3285补充7](https://tech.ebu.ch/docs/tech/tech3285s7.pdf)是chna定义来源。
5. 与一个独立参考renderer比较静态位置/运动位置/范围导出/静音对象，检查downmix及双耳路径能力；不支持的object属性报告并拒绝交付，不能静默删。
6. 工程保存源PCM、原始未知ADM payload和转换报告，cache只存可重建渲染；export临时文件验证后原子发布。

**算法参考：UID与PCM通道映射校验。** 只是引用完整性的最小oracle，不能替代ADM XML/schema/profile validator。

```python
import re

def validate_track_uids(uids: list[tuple[str, int]], channel_count: int) -> None:
    if type(channel_count) is not int or channel_count < 1:
        raise ValueError("invalid PCM channel count")
    names = [uid for uid, _ in uids]
    if len(names) != len(set(names)):
        raise ValueError("duplicate UID")
    for uid, index in uids:
        if re.fullmatch(r"ATU_[0-9A-Fa-f]{8}", uid) is None:
            raise ValueError("invalid UID shape")
        if type(index) is not int or not 0 <= index < channel_count:
            raise ValueError("PCM index out of range")

validate_track_uids([("ATU_00000001", 0), ("ATU_00000002", 1)], 2)
# 不同时间的UID可复用同一PCM轨；XML/profile再验证时间使用合法性。
validate_track_uids([("ATU_00000001", 0), ("ATU_00000002", 0)], 2)
try:
    validate_track_uids([("ATU_00000001", 0), ("ATU_00000001", 1)], 2)
except ValueError:
    pass
else:
    raise AssertionError("duplicate UID accepted")
```

根据[EBU Tech3285-s7 §3.2/§4.3](https://tech.ebu.ch/docs/tech/tech3285s7.pdf)，numUIDs可能大于numTracks，不能把重复PCM index当通用错误。上例内部index从0开始，磁盘chna.trackIndex从1开始；全通道必须被覆盖如为首版限制，应写在具体profile，不当成ADM通用规则。

**验证。** 新建`test_adm_scene_schema.py/test_adm_bwf_roundtrip.py`。fixture每PCM轨独立脉冲、一个静态object、一个跨边界运动object、一个bed；UID引用和声道数100%一致，导入/导出ID稳定。空/重复/悬挂ID、不可信XML、未知profile、缺renderer必须失败。参考renderer对同一版本/配置比float残差，门槛由目标算法与基准建立；声源方向、中心、LFE与运动路径需实际多扬声器/耳机人工核验。外部播放器可读和客户profile验收报告是完成条件，不能以本工具自读自写替代。

**迁移与完成。** 旧2.0/5.1工程不自动变对象，新增scene为空。未知ADM信息保留原件，降级渲染必须显式生成baked asset并报告不可编辑字段。取消释放renderer/临时RF64/WAV；完成需要原生链、profile validator、独立渲染/接收端验证与真实监听证据。

## B19　硬件 Insert 与多路 Cue 输出

**现状与依赖。** 当前 [routing_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/routing_dialog.py)、[routing_matrix_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/routing_matrix_dialog.py) 处理工程轨/Bus路由，并不等于声卡物理输出矩阵。录音已有延迟测量函数，可复用测量方法；没有完整硬件send/return与多路耳机cue服务。依赖B02、B06、B09、B10/B11；交付含硬件处理依赖B21实时打印。工作量20–35人日。

**数据与接口。** `PhysicalPatchProfile(version=1, device_fingerprint, rate, layout, output_channel_map, input_channel_map, exclusive_claims)`放本机；工程`ExternalInsertRef(insert_id, logical_patch_name, calibration_ref, expected_latency_samples, wet, missing_policy)`只存逻辑引用。`CueMix(cue_id, source_taps, send_gains, pre_post, output_patch_ref)`分录音session与工程可移植部分。新增`HardwareInsertProcessor`进入图节点，`CueRenderer`接监听分支，输出统一设备backend；禁止另开多个不同时钟的QAudioSink假装同步。

**实施步骤。**

1. 枚举设备可用channel map，做physical patch占用检查：program、cue、insert校准不能抢同一输出而悄悄求和。允许共享必须显式配置。
2. 选定send/return通道，用低电平短测试脉冲闭环测延迟；复用相关估计，重复多次给分布。calibration绑定设备/采样率/buffer，任一变化立即失效。
3. 外部insert建立send→device→input return节点，音频时钟同源优先；自动测得延迟交B02参与路径补偿。roundtrip jitter超profile容差则拒绝“样本精确”模式。
4. 在硬件send上先设置受控电平与输出mute，明确enable后才送；拒绝可检测的图反馈环。返回信号的gain staging/phase/声道映射独立，不能一边录音一边偷偷覆盖dry源。
5. Cue在所选pre/post tap上取信号，独立音量/pan/mono/DIM；改变cue不能改变Master/Stem。插入/删除cue路由在块边界生效。
6. 导出发现external insert时不能离线假跑；先实时打印其输出，保存源快照/校准/设备和指纹，再由B21受信asset交付。若源工程变了，旧打印失效需重录。

**算法参考：环回延迟估计。** 使用非周期噪声片段避免正弦相关歧义，例子只处理无噪整数延迟。

```python
import numpy as np

def roundtrip_samples(sent: np.ndarray, returned: np.ndarray) -> int:
    if len(sent) == 0 or len(returned) < len(sent):
        raise ValueError("invalid calibration window")
    correlation = np.correlate(returned, sent, mode="valid")
    return int(np.argmax(np.abs(correlation)))

sent = np.random.default_rng(19).normal(size=127)
returned = np.r_[np.zeros(233), sent, np.zeros(100)]
assert roundtrip_samples(sent, returned) == 233
```

生产用带限低电平MLS/噪声，处理background noise、极性反转、search window和次峰置信度；不得给未知外设直接满幅脉冲。

**验证。** 新建`test_external_insert_graph.py/test_cue_outputs.py`。伪device fixture exact delay233、短写、回环断开、声道交换、jitter；声明延迟与脉冲落点≤1样本，patch冲突/未知通道在编译前拒绝。cue mute/solo/volume对Master/Stem样本零影响，离线导出遇未打印insert必阻断。真机至少4输出接口，两个独立耳机cue+stereo program；一次测硬件bypass回环、一次真实处理器，记录校准重复性、实时打印与回放一致性。

**迁移与完成。** 异机打开逻辑patch显示unavailable，不能按相同序号自动连错误设备；旧校准在设备/率/buffer变化时不沿用。取消打印保留原工程、不发布半成品；完成需要物理patch/测量/PDC/断线恢复/多cue隔离和实际打印证据。

## B20　参考 Mix A/B 与声音版本对照

**现状与依赖。** [project_clip_editor.py](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/project_clip_editor.py) 与 [clip_audition.py](/Users/heng/Documents/vscode/topos-studio/src/features/mixer/ui/clip_audition.py) 已有片段级原声/处理后对比，不等于节目reference mix。现有主工程revision/序列存储应复用；不能把另一版WAV作为普通轨永久混入Master。依赖B01、B05、B08、B10；版本打印与B21协作。工作量12–20人日。

**数据与接口。** `ReferenceMixRef(version=1, asset_id, source_start_sample, timeline_anchor_sample, channel_map, original_rate, label, provenance)`；`ComparisonState(active_source, match_gain_db, measurement_key, transition_samples)`仅监听会话。`MixVersion(version_id, source_revision, render_spec_hash, asset_ref, notes, measured_report_ref)`引用可信打印或不可变图快照，原媒体可重连。命令`AddReferenceMixCommand/RemoveReferenceMixCommand/AddMixVersionCommand`只管理工程引用；A/B开关和试听匹配增益不改项目声音模型。

**实施步骤。**

1. reference作为专用监听源加载，明确时间线锚/源入点/TC，不走普通节目轨route。比较前检查布局/采样率，使用共享源解析和resampler。
2. 默认按source time reference或人工锚对齐，给相关估计建议；客户参考有片头/编码延迟时保留显式offset，不能默默trim文件。
3. A/B必须使用同一输出设备时钟/游标，块边界选择源并短crossfade。停止/seek/loop和片段试听仲裁复用现有会话服务。
4. 响度匹配对同一区间、同一布局、同一测量模式和算法版本测量；仅应用到监听比较支路，静音/无效测量禁用match。目标gain需有上限和预计headroom提示；不能通过限幅偷改所谓参考。
5. 版本产生时冻结source revision和render spec；打印完成/校验后登记，后续工程变动不覆盖旧版。用户可比较同区间“当前版/指定旧版/reference”。
6. 版本差异可输出节点/路由/参数/自动化变化列表；音频差分先对齐延迟和布局。含非确定性效果的差异不可归因于单一参数，报告测量条件。
7. Mix Scene Snapshot另存可编辑参数快照与scope/safe masks（如保护routing、某轨、插件或自动化范围）；先preview后复用现有command宏事务召回，只修改选择范围。状态预览与打印版本分开，过期revision/缺插件/锁轨时报告跳过或阻断，不无提示覆盖自动化。测试一次undo恢复全部参数和保护项声音不变。

**算法参考：监听比较增益。** 这是已有合格测量结果的数学转换，不是LUFS测量算法。

```python
import math

def comparison_gain(reference_lufs: float, programme_lufs: float, max_boost_db: float = 12.0) -> float:
    if not (math.isfinite(reference_lufs) and math.isfinite(programme_lufs)):
        raise ValueError("comparison needs finite measurements")
    boost = programme_lufs - reference_lufs
    if boost > max_boost_db:
        raise ValueError("required boost exceeds audition headroom policy")
    return 10.0 ** (boost / 20.0)

assert math.isclose(comparison_gain(-20.0, -23.0), 10.0 ** (-3.0 / 20.0))
```

**验证。** 新建`test_reference_mix_session.py/test_mix_version_roundtrip.py`。fixture原mix、延迟1001样本reference、整体-6dB副本、44.1→48k、不同起点BWF、静音文件；对齐同源脉冲≤1输出样本、有效匹配误差≤0.2LU（建议工程门槛，需独立测量复核）。A/B及match前后Master/Stem样本完全相同；match不入项目undo，不污染工程gain。切换reference→seek→关工程→新工程，旧worker零写回。人工用实际客户ref与旧mix听可感知音量/时基一致，核对metadata与TC；编码有损素材以解码延迟对齐后的容差报告。

**迁移与完成。** 旧片段A/B原样保留；旧工程无reference/version条目不变。missing reference只禁用该比较源，播放当前节目继续；取消打印不生成假版本，资产保留/清理走项目资源策略。完成需要监听隔离、准确锚、有效match、可信版本与保存重开测试。

## 实施顺序与交付检查表

| 波次 | 任务 | 先决条件 | 交付物 |
| --- | --- | --- | --- |
| 1 | B10、B11基础 | B01/B02/B09完成 | 统一监听和可恢复的同设备录音，不急于跨设备 |
| 2 | B13、B14、B20 | 基础监听/时钟/源解析稳定 | 可回退的对白工具、逐句comp、参考比较 |
| 3 | B12 | B04/B05及B21烘焙合同 | 具名接收端AAF子集与真实往返报告 |
| 4 | B15 | 元数据/编辑组/comp规则稳定 | 完整可逆改版事务与实际改版试片 |
| 5 | B19、B17 | 原生多输出与设备时钟稳定 | 物理patch/cue/硬件insert、单协议控制台与外部同步 |
| 6 | B16 | PDC/稳定图/自动化/打印成熟 | 一种格式的隔离host与真实插件兼容表 |
| 7 | B18 | 原生多声道+profile+独立renderer | 明确ADM子集、外部渲染/交接验收 |

各任务完成时在主计划逐行更新“完成/部分/待设备”与证据链接；**不能仅因本文存在、示例能运行或纯函数单测通过就勾选整项完成**。现场验收缺席时记录明确缺口及待测设备/软件，后续按同一fixture补齐，避免又生成一套独立声音系统。
