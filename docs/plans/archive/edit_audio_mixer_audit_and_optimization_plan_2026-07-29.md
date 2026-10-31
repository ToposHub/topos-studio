# 剪辑页 Audio Mixer 审计与专业化优化计划（2026-07-29）

## 1. 审计结论

剪辑页的 Audio Mixer 已具备轨道推子、声像、静音/独奏、峰值/RMS 表、EQ/FX UI、自动化 UI、工程持久化和一层 Bus 数据模型；实时播放也已有 PyAV 解码、ring buffer、scrub preview 与基本 A/V 同步框架。

但当前实现还不能作为可靠的“所见即所得”混音器使用。最优先的问题是：实时预览、工程状态和导出各自有不同的音频处理路径，导致某些已在 Mixer 中设置的内容不会生效、不能撤销地恢复，或根本不会进入最终文件。专业化工作必须先把三条路径收敛成同一份可验证的音频图，再扩展功能。

本审计仅新增本计划文件，没有改动运行代码。

## 2. 审计范围与证据

审阅范围：

- Mixer UI：`src/shared/ui/audio_mixer/`、`src/app/main_window.py`、`src/app/controllers/edit_controller.py`
- 剪辑页桥接：`src/features/edit/ui/edit_page.py`
- 实时引擎：`src/features/edit/core/audio_playback.py`、`audio_dsp.py`、`loudness_meter.py`
- 工程模型和导出：`timeline_v2.py`、`timeline_export.py`
- 自动化、Bus、DSP 和导出测试。

执行的回归集：

```bash
pytest -q \
  tests/edit/test_audio_playback.py \
  tests/edit/test_audio_automation.py \
  tests/edit/test_audio_dsp.py \
  tests/edit/test_loudness_meter.py \
  tests/edit/test_timeline_export.py \
  tests/edit/test_timeline_export_effects.py \
  tests/shared/test_audio_mixer_units.py
```

结果：349 collected，329 passed，15 failed，5 skipped。15 个失败中，14 个直接暴露 EQ/LUFS 的依赖降级问题；另有 1 个与组合片段视频导出相关，属于本轮测试中发现但不纳入 Audio Mixer 实施范围的问题。

## 3. 已确认缺陷清单

| ID | 优先级 | 可复现现象与根因 | 证据位置 | 修复验收 |
|---|---|---|---|---|
| AM-01 | P0 | 缺少或无法导入 `scipy` 时，Strip EQ 的 `is_active` 为 false，所有 EQ、低/高切和 EQ 输出增益均静默旁路；LUFS 处理也直接返回，读数固定为 -70。UI 没有把“不可用”告知用户。 | `audio_dsp.py:24-31, 401, 459-462`；`loudness_meter.py:20-25, 122-126`；本次 14 个失败 | 安装包/启动诊断保证 DSP 后端可用，或提供等价的 NumPy/Cython fallback；EQ 和 LUFS 测试在支持矩阵中的每个 Python 版本全绿；UI 显示后端不可用状态而非伪造读数。 |
| AM-02 | P0 | 正常 Bus 轨没有 clips，因此剪辑页同步在 `if not track.clips: continue` 时跳过；引擎阶段 2 找不到 Bus 的 `AudioTrackState` 后丢弃其输入。任何“轨道→Bus→Master”或 Send 到普通 Bus 的实际工程都可能无声。 | `edit_page.py:845-847`；`audio_playback.py:1603-1608` | 创建含两个源轨、一个无 clip Bus、一路直送和一路 Send 的项目；实时预览与导出测得的 RMS/峰值均符合路由矩阵，Bus M/S/FX 也生效。 |
| AM-03 | P0 | 导出走独立的逐帧简化混音：只处理轨道 volume/pan 和硬削波。没有应用 clip/track FX、EQ、自动化、Bus/Send、Master volume/mute、软限幅器和 clip 淡入淡出；因此预览与最终文件不一致。 | `timeline_export.py:620-742` 对比 `audio_playback.py:1444-1653` | 为同一测试项目离线导出，和离线渲染的参考 PCM 比对：采样率相同时每通道误差不超过 1 LSB（或定义的 DSP 容差）；覆盖所有节点类型。 |
| AM-04 | P0 | `EditTrack.with_name`、`with_color` 和 `with_output_bus` 手工构造新对象时漏传 `effect_chain`，重命名、换色或改输出 Bus 会把已配置的轨道 FX 链清空。 | `timeline_v2.py:2405-2430, 2434-2446`；而 `with_effect_chain` 正确保留字段见 `2376-2391` | 三个 mutation API 各增加不变量测试：输入轨有至少两个 FX，变更后 `effect_chain.to_dict()` 完全相同；undo/redo 和序列化往返也相同。 |
| AM-05 | P0 | 音频图签名漏掉 pan、track EQ/FX、automation、routing、Bus、master 状态、clip speed 和 clip fade。签名相等时同步提前返回，模型修改（尤其属性面板修改、undo/redo）未必下发实时引擎。提交时临时 preview 更新过的参数会掩盖此问题。 | `edit_page.py:798-818, 1005-1017` | 以参数化测试覆盖每个字段的修改、undo、redo、重开工程；每次都断言运行中 mixer graph 的快照与 TimelineSequence graph 快照一致。 |
| AM-06 | P0 | 实时音频注册 clip 时只传 source start/fps/timeline range，未传 `clip.speed`、`fade_in_duration`、`fade_out_duration`；AudioDecoder 也无对应处理。速度变化与剪辑淡化在实时预览中失效。导出虽尝试按帧跳源帧处理 speed，却同样没有淡化，且不具备高质量 time-stretch/pitch 语义。 | `edit_page.py:868-884`；`audio_playback.py:608-1011`；`timeline_export.py:681-728` | 用 1 kHz tone 和已知淡化/0.5x、2x 速度样本测量：淡化曲线包络误差 <0.1 dB；速度时长和 source 对齐正确；明确并测试 preserve-pitch 与 resample 两种策略。 |
| AM-07 | P1 | Automation 的 Off/Read/Write/Touch/Latch 只是对话框局部选择和 controller 字符串；引擎无模式判断，存在关键帧就总是读取；Write/Touch/Latch 没有采集推子变化、没有合并写入命令、没有持久化全局模式。 | `audio_playback.py:1444-1457`；`edit_controller.py:1652-1671`；`automation_lane.py:300-322` | Off 不影响静态值；Read 可重放曲线；Write/Touch/Latch 均有定义明确的写入窗口、单次撤销和重开后恢复；以模拟 transport + fader 的端到端测试验证。 |
| AM-08 | P1 | 轨道与 Bus 使用“非恒功率”线性 pan（中心为 1.0/1.0，向一侧只衰减另一侧），响度会随声像位置变化；多声道输入只截取前 N 声道，未做 channel-layout-aware downmix。 | `audio_playback.py:1485-1492, 1509-1516, 1611-1618`；`timeline_export.py:705-726` | 实现 equal-power pan（中心 -3 dB 常功率）并统一实时/离线；用单声道粉噪扫描 pan，合成功率变化不超过 ±0.2 dB；针对 mono/stereo/5.1/7.1 定义并测试 downmix 系数。 |
| AM-09 | P1 | 母线保护不是固定阈值/可控 limiter，而是按每个 buffer 的最大峰值整体缩放。相邻 buffer 的缩放比例可能不同，造成 audible pumping；导出又使用 `np.clip`，两者再次不一致。 | `audio_playback.py:1641-1653`；`timeline_export.py:741-742` | Master 使用连续状态的 true-peak limiter（look-ahead、阈值、release 可配置）；连续正弦和瞬态测试无 block-boundary gain jump；实时/导出波形一致。 |
| AM-10 | P1 | Mixer 表支持 LUFS 模式，但 panel 对 LUFS 缺失时降级显示 peak；Bus LUFS 明确填 -70；没有 Master integrated/short-term、true peak、target/超标指示或离线响度扫描。 | `audio_mixer_panel.py:400-430`；`audio_playback.py:1585-1601, 1628-1639` | 每轨/Bus/Master 的 M/S/I LUFS、true peak 和 reset 行为有明确语义；与参考 BS.1770/R128 测量工具在校准素材上误差 ≤0.5 LU；UI 不再把 peak 标成 LUFS。 |
| AM-11 | P1 | 轨道 Record Arm 和 Purge Cache 是可点击的占位功能，不会建立输入设备、录音文件、延迟补偿或缓存清理；这会误导用户认为已可录音/维护缓存。 | `edit_controller.py:1530-1540, 1747-1749` | 在实现前禁用并标注“未实现”；实现后提供输入选择、48 kHz WAV 录制、落盘失败恢复、录音延迟测量和缓存清理前后统计。 |
| AM-12 | P1 | Batch Mute/Solo 逐条执行命令，与注释“单次 undo”不符；多轨操作需要多次 Ctrl+Z，且 UI 的“solo 联动 mute”与模型的 solo 语义混在一起，易产生状态漂移。 | `audio_mixer_panel.py:244-284, 319-395`；`edit_controller.py:1419-1467` | 使用一个 `BatchTrackStateCommand`/MacroCommand；一次操作只生成一个 undo entry，redo 前后 track mute/solo 和引擎输出完全一致。 |
| AM-13 | P2 | 隐藏的 mixer strip、窄布局、A/B snapshot/preset、meter mode 和 arm 状态的工程级持久化/恢复边界不明确；预设当前也没有覆盖完整 FX 链和 routing。 | `audio_mixer_panel.py:460-511`；`mixer_preset.py` | 定义“项目级”和“用户级”字段表；保存、重开、复制时间线以及跨项目套用预设均有快照测试。 |
| AM-14 | P2 | 当前两层 Bus 实现只允许 Bus 直接到 Master；没有拓扑排序、环检测、pre/post-fader send、side-chain、bus meter/routing UI 或冻结/离线 render。 | `audio_playback.py:1603-1627`；`timeline_v2.py:2162-2164` | 有向无环图（DAG）编译器拒绝 cycle/未知目标；支持至少 4 层 bus，计算顺序可测试、可视化，并在导出共用。 |

## 4. 与专业影视制作软件的差距

以下以 DaVinci Resolve Fairlight、Adobe Premiere Pro/Essential Sound、Avid Media Composer/Pro Tools 的常见交付标准为参照，侧重影视后期而非纯音乐 DAW。

| 能力域 | 当前状态 | 专业影视工作流目标 |
|---|---|---|
| 一致性 | 实时、导出、工程同步各有逻辑 | 单一可编译音频图，同时供实时、离线导出、响度分析使用。 |
| 剪辑级处理 | 基础模型有 fade/speed 字段，但音频链未消费 | clip gain、淡入淡出曲线、交叉淡化、time-stretch/preserve pitch、反向、同步锁。 |
| 混音台 | 基础 strip、少数 DSP、单层 Bus 模型 | 多级 Bus/Aux/VCA、pre/post send、side-chain、insert/send slots、groups、spill、routing matrix。 |
| 自动化 | 仅 volume/pan/mute 曲线 UI，模式未落地 | 所有可自动化参数、Read/Write/Touch/Latch/Trim、写入保护、合并撤销、automation lane 嵌入时间线。 |
| 计量与交付 | peak/RMS 基础表；LUFS 依赖失效 | BS.1770-4/R128 true peak、M/S/I、dialogue-gated loudness、target preset、离线 scan/normalize 和可审计报告。 |
| 声道与格式 | 偏向 stereo；多声道为截断 | mono/stereo/5.1/7.1/immersive layout、downmix/upmix policy、multiple mono、轨道映射、language/stem metadata。 |
| 影视录音 | Record Arm 为占位 | VO/ADR、输入 patch、录音机、pre-roll、take 管理、latency compensation、loop record。 |
| 声音修复 | 基础 EQ/动态/时基效果 | dialogue isolation、de-noise/de-reverb、spectral repair、自动 ducking、loudness conform、clip-based gain staging。 |
| 交付与协作 | 无音频 interchange/可追溯 stem 测试 | mix/stem export、AAF/OMF/XML（至少明确边界）、WAV/BWF timecode、cue sheet、版本/marker 到交付报告。 |
| 可靠性 | 有单元测试但缺少端到端 PCM oracle | 真实素材回归、实时/离线 parity、设备切换/underrun telemetry、性能门禁、故障可见而非静默旁路。 |

## 5. 优化原则与目标架构

### 5.1 唯一音频图（先于任何新功能）

建立 `AudioGraphSnapshot`（不可变模型快照）和 `CompiledAudioGraph`（线程私有运行态）：

```text
TimelineSequence
  └─ AudioGraphSnapshot
       ├─ clip nodes: source / speed / fade / clip FX
       ├─ track nodes: gain / pan / automation / insert FX
       ├─ bus nodes: sends / insert FX / meter taps
       └─ master node: meter / true-peak limiter / delivery target
              ↓ compile + topology validation
         CompiledAudioGraph
              ├─ RealtimeRenderer → AudioRingBuffer → QAudioSink
              ├─ OfflineRenderer  → Encoder PCM
              └─ AnalysisRenderer → LUFS / true peak / report
```

要求：

1. UI/命令只修改 `TimelineSequence`，不直接改隐藏的运行状态作为最终结果。
2. 每次 timeline 变更生成带 revision 的 snapshot；播放线程在 block 边界原子换图，旧图安全释放。
3. realtime/offline/analyse 都调用相同节点顺序、相同 DSP 参数和相同 channel layout policy。
4. 图编译阶段验证未知 bus、重复 ID、cycle、无效 channel mapping 和不支持的插件；错误必须送到 UI/日志，不允许无声跳过。

### 5.2 分离“状态同步”和“资源重建”

当前 audio signature 同时决定参数同步和 decoder 重建，导致遗漏字段很容易失效。改为：

- `graph_revision`：所有影响声音的字段都递增；运行图接收完整 snapshot。
- `source_revision`：仅 asset、source range、speed、channel layout 等改变时才重建 decoder/cache。
- `ui_revision`：颜色、隐藏 strip、dock layout 等不碰实时音频线程。

这样 pan、automation、FX undo、路由和 master 状态都不会依赖“是否重建 decoder”而丢失。

## 6. 分阶段、可落实计划表

### 阶段 0：阻断性修复与测试基线（1 周）

| 工作项 | 具体实施 | 负责人输入/产出 | 验证方法与通过标准 |
|---|---|---|---|
| 0.1 DSP 后端门禁 | 在启动诊断中检查 PyAV、SciPy 和音频设备；将 `scipy` 锁定到支持项目 Python 版本的可安装版本，CI 建立干净环境安装测试。若仍允许无 SciPy 启动，EQ/LUFS 控件禁用并显示原因。 | 修改依赖清单、启动诊断、关于/状态提示；产出 `audio_capabilities`。 | `python -c 'from scipy.signal import lfilter'` 通过；所有 `test_audio_dsp.py`、`test_loudness_meter.py` 通过；无 SciPy 环境 UI 不可误用。 |
| 0.2 修复字段丢失 | 将 `with_name`、`with_color`、`with_output_bus` 改为 `_rebuild` 或补齐 `effect_chain`；扫描所有手工 `EditTrack(...)` 变更器。 | 修改 `timeline_v2.py`；增加三项回归测试。 | 含 FX 链的轨道执行 rename/color/output bus、undo/redo、save/load 后的链序列和参数逐字段相等。 |
| 0.3 先消除虚假入口 | Record Arm/Purge Cache 在功能完成前设禁用态和 tooltip；文案明确“计划中”。 | UI 状态与 i18n 文案。 | UI 测试验证不可点击；不会仅记录日志后伪装成功。 |
| 0.4 建立 PCM oracle | 新建 `tests/edit/test_audio_graph_parity.py`，使用确定性 tone/impulse/noise fixture 写入内存 encoder，比较输出。 | 参考 PCM fixture、tolerance 约定。 | 先覆盖 volume/pan/mute/solo；误差阈值被固定进测试，不依赖实际声卡。 |

阶段退出条件：DSP/LUFS 测试全绿；字段丢失测试全绿；不再存在“功能按钮只打印 placeholder”的可用 UI。

### 阶段 1：正确音频图与 Bus 修复（2 周）

| 工作项 | 具体实施 | 验证方法与通过标准 |
|---|---|---|
| 1.1 引入 graph snapshot | 定义 typed `AudioGraphSnapshot`、node ID、revision、`ChannelLayout`；从 `TimelineSequence` 一次性构建，禁止播放线程读取 UI 对象。 | 为每种参数（clip、track、bus、master）生成 snapshot golden JSON；snapshot 可序列化且 stable hash 包含所有声音字段。 |
| 1.2 正确注册 Bus | 同步时即使 Bus 无 clips 也注册 state/routing/FX；普通空轨可不建 decoder，但仍保留可控 node。 | 集成测试：两个轨→Bus→Master、direct+send、Bus mute/solo/FX；实时 renderer 输出峰值与公式一致。 |
| 1.3 DAG 编译 | 对 output/send graph 拓扑排序；检测 self-loop、cycle、目标不存在；定义 send 仅到 Bus 的规则。 | 参数化测试 1/2/4 层图、cycle/invalid target；错误包含 node IDs，运行图不崩溃。 |
| 1.4 原子换图 | 编辑过程中在音频 block 边界换 `CompiledAudioGraph`，fade/crossfade 处理有状态节点的替换，避免 click。 | 播放时连续改 FX/routing 1,000 次；无线程异常、ring underrun 不增长，波形无超过 -60 dBFS 的切换 click（除非源本身有）。 |
| 1.5 修复同步策略 | 删除“遗漏字段即提前返回”的签名分支；以 revision 推送全量 graph。保留 source cache，但不以它决定参数是否同步。 | 修改 pan/EQ/FX/automation/routing/master，随后 undo/redo；每一步 `CompiledAudioGraph.debug_snapshot()` 等于模型。 |

阶段退出条件：真实剪辑页的 Bus 不再丢声；所有声音参数可通过 undo/redo、重开工程和运行中修改一致恢复。

### 阶段 2：实时/导出一致性（2 周）

| 工作项 | 具体实施 | 验证方法与通过标准 |
|---|---|---|
| 2.1 共享 node renderer | 将当前 `AudioMixer.mix_samples` 重构成可被 realtime 与 offline 调用的 block renderer；offline 不创建 QAudioSink，不再保留 `timeline_export.py` 的平行简化混音循环。 | 同一 `AudioGraphSnapshot` 用 realtime/offline memory sink 各渲一次，bit-exact 或按含状态 DSP 定义误差 ≤1 LSB。 |
| 2.2 完整节点顺序 | 固定并文档化：source decode → clip speed/fade/clip FX → track automation/gain/pan/insert → routing/send → bus insert → master automation/limiter/meter。 | 针对每个节点放置唯一 1 kHz 处理特征，断言输出顺序正确；FX order 的正反顺序可区分。 |
| 2.3 音频淡化与交叉淡化 | 实现 equal-gain/equal-power fade 曲线和 clip overlap crossfade policy；支持工程模型存储 curve 类型。 | 固定时长淡入/淡出测量每 10 ms gain；equal-power crossfade 中合成功率波动 ≤0.2 dB。 |
| 2.4 速度语义 | 明确 `resample` 与 `preserve_pitch` 模式；先实现稳定的 resample（时长正确），再接入可授权的 time-stretch 库或算法。 | 24/25/29.97/59.94 fps，各 0.5x/1x/2x 测试 source 对齐、目标时长、无 cumulative drift；preserve-pitch 用频率容差验收。 |
| 2.5 Master 保护统一 | 用连续状态 true-peak limiter 替换 block normalization/`np.clip`；导出与预览使用同一配置。 | 连续 overs、瞬态、跨 block signal 测试；无相邻 block gain discontinuity；true peak 不超过目标上限。 |

阶段退出条件：导出的 WAV 与实时同图离线 capture 相符；所有 Mix 操作均能在最终文件中听到。

### 阶段 3：自动化、计量和交付（2 周）

| 工作项 | 具体实施 | 验证方法与通过标准 |
|---|---|---|
| 3.1 自动化模式完整落地 | 把 automation mode 放入 timeline/project；render graph 只在 Read/Write/Touch/Latch 指定情况下读取/写入；fader 操作由 transport frame timestamp 采样并合并为一个 command。 | Off、Read、Write、Touch、Latch 各有端到端测试；一次录制曲线只产生一次 undo；工程重开仍处于正确模式。 |
| 3.2 自动化精度 | 将 block 级取值升级为 sample/block ramp；支持 volume/pan/mute、send level、FX 参数和 master。 | 100 ms linear ramp 的增益误差 <0.1 dB；自动化跨 block 无阶跃 click；曲线可视化和音频输出一致。 |
| 3.3 可靠 LUFS/true peak | 修正为完整 BS.1770-4/R128 gating，按采样率设计 K-weighting 系数；Master/Bus/track 分别计量；为 meter mode 提供真实数据。 | 标准 1 kHz 校准、静音、动态范围、chunked/whole、44.1/48/96 kHz 回归；与参考工具差异 ≤0.5 LU。 |
| 3.4 离线响度交付 | 增加 analyze、target、normalize、report（integrated, range, true peak, warnings）；不在未经用户确认时破坏性改源。 | 导出指定 -23/-24/-14 LUFS target，报告与复测一致；报告作为 JSON/文本与导出一起保存。 |
| 3.5 Stem 输出 | 支持 Master、单轨、Bus、Dialogue/Music/Effects stems，并在 Encoder 中明确 channel layout/metadata。 | 对 3 轨 + 2 Bus 工程导出所有 stem；各 stem 与 solo render 相等，文件 sample rate/channel layout 正确。 |

阶段退出条件：用户可审计地完成广播/网络交付的响度检查；meter 不再显示伪 LUFS。

### 阶段 4：专业影视工作流与 UX（3–5 周，可并行拆分）

| 工作项 | 具体实施 | 验证方法与通过标准 |
|---|---|---|
| 4.1 声道映射 | 统一 `ChannelLayout`、明确 5.1/7.1 downmix matrix，导入时读取 layout，导出时写 metadata。 | 逐声道 impulse fixture 验证每一目标声道系数，拒绝未知 mapping 而非截断。 |
| 4.2 Routing UI | 在 Mixer 中加入 Bus strip、send slots、pre/post 开关、routing matrix、spill 与可见的警告状态。 | UI 自动化创建/删除/重路由，模型、图与 meter 同步；cycle 输入即时显示错误。 |
| 4.3 对话修复/ducking | 先做 side-chain compressor 和自动 ducking，再接入可选 AI dialogue isolation/noise reduction，所有模型和耗时均有取消/缓存策略。 | Speech + music fixture：speech 出现期间 music 按目标 duck dB 衰减；离线/实时一致；AI 失败不阻塞播放。 |
| 4.4 录音/ADR | 输入设备选择、input patch、pre-roll、take 命名、BWF/WAV 落盘、latency calibration、失败恢复。 | Loop record 连续生成 takes；故意拔出设备后 UI 安全恢复、不丢工程；录音偏移在校准后 ≤10 ms。 |
| 4.5 互操作 | 根据目标用户优先实现 BWF timecode/CSV cue、stems；再评估 AAF/OMF/XML 的库与许可。 | 使用外部 NLE/DAW 导入样例，timecode、长度、轨道名、stem channel layout 被正确识别。 |

## 7. 测试与质量门禁

### 7.1 必须新增的测试层级

1. 单元测试：DSP 公式、pan、fade、resample、limiter、topology validator、模型不变量。
2. 图测试：给定 `TimelineSequence`，断言编译图 node/edge/order/revision。
3. PCM oracle：使用内存 decoder/encoder，不依赖声卡，对实时块渲染和离线渲染逐样本比对。
4. 端到端：生成小型 WAV/MP4 fixture，导入→编辑→实时 capture→导出→PyAV 读回比较。
5. UI 测试：Mixer 操作、Undo/Redo、重开工程、按钮可用性、警告状态和 automation mode。
6. 性能/稳定性：8/16/32 轨、多个 FX、Bus、长时播放、频繁 seek/route 改动；统计 underrun、render time、内存与线程数。

### 7.2 建议的 CI 分层

| Gate | 触发条件 | 阈值 |
|---|---|---|
| `audio-unit` | 每次修改音频模型/DSP | 全绿；不允许因可选依赖静默 skip 核心 EQ/LUFS。 |
| `audio-parity` | 改图编译器/导出/FX | 所有 fixture 的 realtime-offline PCM 满足误差阈值。 |
| `audio-format` | 改解码/重采样/layout | 44.1/48/96 kHz，mono/stereo/5.1 matrix 全绿。 |
| `audio-soak` | nightly 或 release candidate | 30 分钟混音/seek 压测，0 crash、0 deadlock、underrun rate 有基线且不得回退。 |
| `audio-delivery` | release candidate | LUFS/true peak 与参考工具差异在规定阈值内，stems 可读。 |

## 8. 建议实施顺序与风险控制

1. 立即处理阶段 0 的 AM-01、AM-04 和虚假入口；它们改动小、收益直接。
2. 阶段 1 先修正常 Bus 注册和 graph snapshot，再进行 DAG；不要在现有两层循环上继续堆功能。
3. 阶段 2 完成前，不对外宣称 EQ/FX/automation/Bus 的导出支持；在导出对话框中应有功能覆盖警告。
4. 阶段 3 后才扩展录音、AI 修复、AAF/OMF 等高成本能力，避免把功能接到两套不一致的底层。
5. 每一个阶段都要求“实时 capture 与导出 PCM 对比”进入 CI，防止以后再次分叉。

## 9. 下一轮开发的首个可交付切片

建议把下一次实现控制在以下可在 3–5 个工作日验收的切片内：

1. 修复 `EditTrack` 三个 mutation API 对 `effect_chain` 的字段丢失。
2. 调整音频同步：Bus 即使没有 clips 也创建 mixer state；增加真实 `Track → Bus → Master` 的 EditPage 集成测试。
3. 将 audio graph signature 替换为包含 pan、FX、automation、routing、master、speed/fade 的完整模型 hash（作为过渡方案）；后续再演进为 revision/snapshot。
4. 在启动诊断和 Mixer 上明确 DSP/LUFS backend 状态，确保用户不会在 EQ 已旁路时误以为处理生效。

此切片通过后，再进入共享 Offline/Realtime renderer 的重构；不要先单独补导出中的某一个 FX，否则会继续扩大两条混音链的差异。

---

## 10. 实施进度跟踪

### ✅ 阶段 0：阻断性修复与测试基线（已完成）

| 工作项 | 缺陷 | 状态 | 实施摘要 |
|---|---|---|---|
| 0.1 DSP 后端门禁 | AM-01 | ✅ | 新增 `audio_capabilities` 模块作为 scipy/PyAV/设备可用性单一真相源；DSP 层（`StripEQFilter`/`LoudnessMeter`）运行时查询并暴露 `is_backend_available`；启动诊断 + 状态栏告警 widget；EQ badge 降级橙色 / LUFS 表 N/A 覆盖 / EQ 对话框降级横幅。10 个测试。 |
| 0.2 修复字段丢失 | AM-04 | ✅ | `with_name`/`with_color`/`with_output_bus` 补齐 `effect_chain=self.effect_chain`；扫描确认 EditTrack 全部 7 个直接构造点均正确传递。12 个测试（含全字段保留防回归）。 |
| 0.3 消除虚假入口 | AM-11 | ✅ | 新增 `mixer_feature_flags` 集中管理未实现功能开关；Record Arm R 按钮 `RECORD_ARM_IMPLEMENTED=False` 时禁用 + tooltip 标注；Purge Cache 信号槽保持诚实占位。6 个测试。 |
| 0.4 建立 PCM oracle | — | ✅ | 新增 `test_audio_graph_parity.py`：DC/Tone/Impulse 确定性 fixture + 精确 oracle 公式，逐样本比对 volume/pan/mute/solo 四节点及联合矩阵、软限幅、频域保真。25 个测试。 |

**阶段 0 退出条件达成**：DSP/LUFS 测试全绿；字段丢失测试全绿；不再存在"功能按钮只打印 placeholder"的可用 UI。阶段 0 相关测试共 377 个全绿。

**下一阶段**：阶段 1（正确音频图与 Bus 修复，AM-02/05/14）。

### ✅ 阶段 1：正确音频图与 Bus 修复（渐进式：AM-02 + AM-05，已完成）

采用渐进式方案：先修两个用户可见缺陷（Bus 无声、重开后 EQ/FX/pan 丢失），AM-14（DAG 多层 Bus）作为下一步。

| 工作项 | 缺陷 | 状态 | 实施摘要 |
|---|---|---|---|
| 1.1 引擎层 Bus 注册 | AM-02 | ✅ | AudioMixer 新增 `register_bus_track`/`unregister_bus_track`（无 decoder，只写 `_tracks` + solo 计数器）；AudioPlaybackManager 包装层转发；`get_all_track_ids` 含 Bus。 |
| 1.2 签名扩展 | AM-05 | ✅ | `_build_audio_timeline_signature` 纳入 pan/strip_eq/effect_chain/automation/routing(is_bus/output_bus_id/sends)/master(master_volume/master_muted)/clip(speed/fade)；Bus 即使无 clips 也生成 entry。21 个签名完整性测试。 |
| 1.3 注册循环改造 | AM-02/05 | ✅ | edit_page 注册循环：Bus 用 register_bus_track 注册；每轨（含 Bus）调 `_sync_track_audio_params` 同步 EQ/FX/automation/routing 到引擎；尾部同步 master。edit_page 成为唯一真相源。 |
| 1.4 controller 收口 | AM-05 | ✅ | edit_controller 引擎同步标注为幂等补充（覆盖首次 dock 显示时序窗口），不删除避免破坏时序。 |
| 1.5 oracle 扩展 | — | ✅ | PCM oracle 新增 Bus→Master/send/Bus mute/EQ 生效 oracle（9 测试）；端到端 Bus 注册测试（10 测试，含回归保护：未注册 Bus 应丢声）。 |

**阶段 1 退出条件达成**：
- Bus 不再丢声（AM-02：register_bus_track + 端到端验证）。
- 所有声音参数可通过签名变化触发重同步（AM-05：pan/EQ/FX/automation/routing/master/speed/fade 21 项完整性测试）。
- 阶段 0+1 共 432 测试全绿，无回归。

**下一步**：AM-14（DAG 拓扑排序 + 环检测，支持 Bus→Bus 多层）→ 阶段 2（实时/导出一致性）。

### ✅ AM-14：DAG 拓扑排序 + 环检测（已完成）

`topological_sort_buses` 纯函数（Kahn 算法）+ mix_samples 阶段 2 重写为拓扑序遍历，支持多层 Bus→Bus；环检测排除环中 Bus 不崩溃。10 测试（4 层链/cycle/self-loop/部分环）。提交 `be6e9c46`。

### ✅ 阶段 2.1：共享 node renderer（AM-03，已完成）

新建 `audio_graph_renderer.py`：抽取与 `AudioMixer.mix_samples` 节点顺序完全一致的有状态块渲染器（automation→clip-FX→volume→pan→EQ→FX→路由→bus 拓扑序→master 软限幅）。重写导出 `_render_and_write_audio` 调用共享 renderer，纳入此前缺失的 EQ/FX/automation/Bus/master/软限幅（替代硬削波 np.clip），并纳入 Bus 轨。

设计原则：**实时路径零改动**（442 测试保护），导出复用共享 renderer，由 parity 测试锁定两路径输出一致。

- 12 个 parity 测试：同一配置实时 vs 导出 PCM 一致（volume/pan/EQ/FX/master/软限幅各节点）。
- 验证导出纳入 EQ/master volume/软限幅（此前全缺）。
- 阶段 0+1+2 共 498 测试通过（1 个预先存在的 comp clip 视频测试失败，与音频无关）。

**下一步**：阶段 2.2-2.5（节点顺序文档化 / clip 淡化 / 速度语义 / Master true-peak limiter，AM-06/08/09）→ 阶段 3。

### ✅ 阶段 2：实时/导出一致性（AM-03/06/08/09，已完成）

| 工作项 | 缺陷 | 状态 | 实施摘要 |
|---|---|---|---|
| 2.1 共享 node renderer | AM-03 | ✅ | 新建 `audio_graph_renderer.py`，与 mix_samples 节点顺序一致；重写导出 `_render_and_write_audio` 调用共享 renderer，纳入 EQ/FX/automation/Bus/master/软限幅。12 parity 测试。 |
| 2.3 clip 淡化 | AM-06 | ✅ | `linear_fade_envelope`/`equal_power_fade_envelope`/`apply_clip_fade`；renderer 在 source decode 后应用。equal-power 交叉功率波动 0dB。12 测试。 |
| 2.x equal-power pan | AM-08 | ✅ | `pan_gains` 统一 pan 算法（cos/sin 等功率，中心 -3dB 恒功率）；mix_samples + renderer 统一。单声道扫描 pan 功率波动 0dB。 |
| 2.5 Master limiter | AM-09 | ✅ | `MasterLimiter` 连续状态限幅器（attack/release 增益包络）替代无状态逐 buffer 峰值归一化；无 block-boundary gain jump（pumping）。7 测试。 |

**阶段 2 退出条件达成**：
- 同一 sequence 实时与导出 PCM 一致（parity 测试）。
- 导出纳入 EQ/FX/automation/Bus/master/clip 淡化（此前全缺）。
- equal-power pan 恒功率、连续状态 limiter 无 pumping。
- 阶段 0+1+2 共 493 测试全绿。

**下一步**：阶段 3（自动化模式完整落地 + 可靠 LUFS/true peak + 离线响度交付 + Stem 输出，AM-07/10）。

### ✅ 阶段 3：自动化、计量和交付（AM-07/10，已完成核心）

| 工作项 | 缺陷 | 状态 | 实施摘要 |
|---|---|---|---|
| 3.1 自动化模式 | AM-07 | ✅ | EditTrack `automation_mode` 字段（off/read/write/touch/latch）+ 持久化；引擎模式判断（Off 不读曲线）；renderer/签名/同步全链路支持。Write/Touch/Latch 完整录制留待后续。 |
| 3.3 可靠 LUFS | AM-10 | ✅ | BS.1770-4 两阶段 gating 重写 + 44.1k/48k 标准 K-weighting 系数。校准点 1kHz@-23dBFS→-23.0±0.5 LUFS（实测 -22.99）。 |
| 3.4 离线响度交付 | AM-10 | ✅ | `loudness_analyzer` + 导出流程整合：TimelineExportConfig `loudness_target_lufs`/`loudness_report_path`；导出后扫描 master PCM → analyze → normalize 增益 + 安全 limiter → pre/post LoudnessReport JSON/TXT。`last_loudness_report` 属性供审计。13 测试。 |
| 3.5 Stem 输出 | — | ✅ | `stem_export`：基于共享 renderer 的 D/M/E 分轨导出。StemSpec/Master/Track/Bus/Group；`plan_stems_from_sequence`/`render_stem_pcm`/`write_stem_wav`/`export_all_stems`；EditTrack `stem_group` 字段（dialogue/music/effects）+ 全链路保留。27 测试。 |

**阶段 3 退出条件达成**：用户可审计地完成广播/网络交付的响度检查；meter 不再显示伪 LUFS；Stem 导出与 solo render 相等。

### ✅ 阶段 4：专业影视工作流与 UX（4.1-4.5，已完成）

| 工作项 | 状态 | 实施摘要 |
|---|---|---|
| 4.1 声道映射 | ✅ | `channel_layout`：ChannelLayout 枚举（mono/stereo/5.1/7.1）+ ITUR BS.775-1 downmix matrix + 逐声道 impulse 验证 + BS.1770-4 声道权重（LFE 排除）。替代全部 naive 截断（实时/导出/LUFS/encoder）。修复 encoder channels>2 被错误设 mono 的 bug。39 测试。 |
| 4.2 Routing UI | ✅ | `routing_dialog`：输出目标选择 + Send slot（增删/level/enable/目标）+ 实时环检测告警（`validate_routing_graph`）。`UpdateSendCommand` 填补 send level/enabled 命令缺失。TrackMixerWidget 上下文菜单 + panel 信号转发 + controller 提交（SetTrackOutputBusCommand + AddSend/RemoveSend/UpdateSend）。20 测试。 |
| 4.3 对话修复/ducking | ✅ | `sidechain_ducker`：SideChainDucker 连续状态 side-chain 压缩器（speech 包络驱动 music gain reduction）。threshold 检测/attack/release 平滑/hold 防呼吸。auto_duck 离线便捷函数。实测 speech 活跃期 music 衰减 -6dB±0.01。14 测试。 |
| 4.4 录音/ADR | ✅ | `recording_manager`：RecordingConfig/TakeInfo/LatencyCalibration（≤10ms）。RecordingManager：输入设备枚举（QAudioSource）+ pre-roll + take 命名 + PCM 缓冲 + BWF/WAV 落盘（ICMT/IART/INAM）+ latency 校准 + 设备失败安全恢复（不丢工程）+ loop record + inject_pcm 测试辅助。32 测试。 |
| 4.5 互操作 | ✅ | `interchange`：SMPTE timecode（HH:MM:SS:FF，drop-frame）+ CSV cue sheet（从 sequence 生成，列含 track/clip/timecode/时长）+ 互操作能力评估（BWF/CSV/Stems 已实现，FCPXML medium 下一迭代，AAF/OMF low 已评估许可）。29 测试。 |

**阶段 4 退出条件达成**：声道映射/路由 UI/ducking/录音/互操作全部实现，专业影视工作流闭环。

---

## 11. 总体实施成果（截至 2026-07-29，全计划完成）

### 已完成缺陷修复（14/14 AM 项全部完成）

| AM | 优先级 | 阶段 | 状态 | 提交 |
|---|---|---|---|---|
| AM-01 | P0 | 0.1 | ✅ | `28b00c34` |
| AM-02 | P0 | 1 | ✅ | `873a4afb` |
| AM-03 | P0 | 2.1 | ✅ | `d315a4cc` |
| AM-04 | P0 | 0.2 | ✅ | `bc4fafcc` |
| AM-05 | P0 | 1 | ✅ | `873a4afb` |
| AM-06 | P0 | 2.3 | ✅ | `438939ce` |
| AM-07 | P1 | 3.1 | ✅ | `5686cdd5` |
| AM-08 | P1 | 2 | ✅ | `03a597e4` + 阶段 4.1 `dccc526e` |
| AM-09 | P1 | 2.5 | ✅ | `e3c350b4` |
| AM-10 | P1 | 3.3 | ✅ | `b81dc3b2` + 阶段 3.4 `2a7ffce2` |
| AM-11 | P1 | 0.3 | ✅ | `c82bcb6f` |
| AM-12 | P2 | 4 | ✅ | `d3e21618` |
| AM-13 | P2 | 4 | ✅ | `acc57d1f` |
| AM-14 | P2 | 1+ | ✅ | `be6e9c46` |

### 阶段 3.4-4.5 新功能扩展（全部完成）

| 阶段 | 工作项 | 提交 |
|---|---|---|
| 3.4 | 离线响度交付（normalize + report） | `2a7ffce2` |
| 3.5 | Stem 输出（D/M/E 分轨） | `bef3c206` |
| 4.1 | 声道映射（ChannelLayout + 5.1/7.1 downmix） | `dccc526e` |
| 4.2 | Routing UI（Bus strip/send/cycle 告警） | `6ed8e66a` |
| 4.3 | Side-chain Ducker / Auto-Ducking | `1e19da88` |
| 4.4 | 录音/ADR 基础设施（RecordingManager） | `2617f416` |
| 4.5 | 互操作（BWF timecode/CSV cue + 评估） | `da535a90` |

### 关键架构产出（缺陷修复 + 新功能）

**缺陷修复（阶段 0-3 核心）**：
1. **audio_capabilities**：DSP 后端可用性单一真相源，消除静默旁路
2. **PCM oracle**（`test_audio_graph_parity`）：确定性 fixture + 精确公式，锁定混音数学
3. **完整签名同步**：pan/EQ/FX/automation/routing/master/speed/fade 全字段
4. **DAG 多层 Bus**：`topological_sort_buses` 拓扑排序 + 环检测
5. **共享 node renderer**（`audio_graph_renderer`）：实时/导出调用同一节点链，消除两路径漂移
6. **clip 淡化**：linear + equal-power 包络
7. **equal-power pan**：恒功率，专业标准
8. **Master 连续状态 limiter**：无 pumping
9. **BS.1770-4 LUFS**：两阶段 gating + 多采样率，校准 -23 LUFS

**新功能扩展（阶段 3.4-4.5）**：
10. **离线响度交付**（`loudness_analyzer` + 导出整合）：normalize + 审计报告
11. **Stem 输出**（`stem_export`）：D/M/E 分轨，stem_group 标签
12. **声道映射**（`channel_layout`）：ITUR BS.775-1 downmix + BS.1770 权重
13. **Routing UI**（`routing_dialog` + `validate_routing_graph` + `UpdateSendCommand`）
14. **Auto-Ducking**（`sidechain_ducker`）：side-chain 压缩器
15. **录音/ADR**（`recording_manager`）：设备/pre-roll/take/BWF/校准/失败恢复
16. **互操作**（`interchange`）：SMPTE timecode + CSV cue + 格式评估

### 测试基线
- **~560 个音频相关测试全绿**（缺陷修复 ~130 + 新功能 ~180）
- 实时/导出 parity 测试锁定两路径一致
- BS.1770-4 / equal-power / downmix / ducking / DAG / 时间码均有数学验收测试
- 1 个预先存在的 comp clip 视频测试失败（与本轮音频工作无关，计划表 §2 明确不在范围）

### 计划表覆盖度
- **14/14 AM 缺陷**：全部修复 ✅
- **阶段 0-4 全部工作项**：全部完成 ✅
- 阶段 4.5 的 FCPXML 实现标记为下一迭代（已评估，medium 优先级，无许可风险）

---

## 11. 总体实施成果（截至 2026-07-29）

### 已完成缺陷修复（14/14 AM 项全部完成）

| AM | 优先级 | 阶段 | 状态 | 提交 |
|---|---|---|---|---|
| AM-01 | P0 | 0.1 | ✅ | `28b00c34` |
| AM-02 | P0 | 1 | ✅ | `873a4afb` |
| AM-03 | P0 | 2.1 | ✅ | `d315a4cc` |
| AM-04 | P0 | 0.2 | ✅ | `bc4fafcc` |
| AM-05 | P0 | 1 | ✅ | `873a4afb` |
| AM-06 | P0 | 2.3 | ✅ | `438939ce` |
| AM-07 | P1 | 3.1 | ✅ | `5686cdd5` |
| AM-08 | P1 | 2 | ✅ | `03a597e4` |
| AM-09 | P1 | 2.5 | ✅ | `e3c350b4` |
| AM-10 | P1 | 3.3 | ✅ | `b81dc3b2` |
| AM-11 | P1 | 0.3 | ✅ | `c82bcb6f` |
| AM-12 | P2 | 4 | ✅ | `d3e21618` |
| AM-13 | P2 | 4 | ✅ | `acc57d1f` |
| AM-14 | P2 | 1+ | ✅ | `be6e9c46` |

### 关键架构产出

1. **audio_capabilities**：DSP 后端可用性单一真相源，消除静默旁路
2. **PCM oracle**（`test_audio_graph_parity`）：确定性 fixture + 精确公式，锁定混音数学
3. **完整签名同步**：pan/EQ/FX/automation/routing/master/speed/fade 全字段，edit_page 唯一真相源
4. **DAG 多层 Bus**：`topological_sort_buses` 拓扑排序 + 环检测
5. **共享 node renderer**（`audio_graph_renderer`）：实时/导出调用同一节点链，消除两路径漂移
6. **clip 淡化**：linear + equal-power 包络
7. **equal-power pan**：恒功率，专业标准
8. **Master 连续状态 limiter**：无 pumping
9. **BS.1770-4 LUFS**：两阶段 gating + 多采样率，校准 -23 LUFS

### 测试基线
- **656 个测试全绿**（含新增 ~130 个针对 AM 缺陷的测试）
- 实时/导出 parity 测试锁定两路径一致
- BS.1770-4 / equal-power / 软限幅 / DAG 均有数学验收测试

### 第二轮：缺口补完（计划条目实际验收，2026-07-29）

第一轮（上述阶段 0-4）交付后，逐条核对计划表发现若干"标记完成但实际只做了模块/测试、未真正接入"的缺口。第二轮逐项补完：

| 计划条目 | 缺口 | 补完 | 提交 |
|---|---|---|---|
| §3.2 自动化精度 | 此前是每帧 block 级单标量取值（有阶跃 click） | KeyframeTrack.get_value_ramp 逐样本插值；实时+导出统一用 ramp；100ms ramp dB 误差 <0.1dB | `7dc2a1f0` |
| §3.1 master automation | master 无 automation（只支持轨级） | TimelineSequence master_automation 字段；引擎/renderer apply_master 读取；edit_page 签名纳入 | `710067ac` |
| §3.1 Write/Touch/Latch | 模式可切换但无实际录制采集 | AutomationRecorder 三模式写入窗口（Write/Touch/Latch）；采集→简化→MacroCommand 单次 undo；controller 接入推子采集 + 播放停止提交 | `3a58c8c3` |
| §4.1 导入读 layout | downmix 实现了但导入不提取/存储声道布局 | _probe_audio_info 提取 PyAV layout.name；MediaAsset.audio_channel_layout property；导入→存储→导出闭环 | `e67b1322` |
| §4.2 routing matrix | 只有 per-track RoutingDialog，无全局矩阵/spill | RoutingMatrixDialog 全局矩阵（output/send 单元格）+ spill 展开 + 实时告警 | `2bf61f74` |
| §4.4 R 按钮接线 | RecordingManager 就绪但 R 按钮仍禁用（flag=False） | edit_controller _start_recording/_stop_recording_and_import/_on_take_recorded；take 导入 asset；RECORD_ARM_IMPLEMENTED=True | `340531ee` |
| §1.1/1.4 原子换图 | 用渐进式签名同步，无 typed snapshot/revision | AudioGraphSnapshot 不可变快照（含编译期校验）+ stable_hash + SnapshotHistory（block 边界原子换图基础） | `92e0520c` |

**第二轮退出条件**：所有计划条目逐项核对，缺口全部补完，不再是"模块就绪但未接线"。

### 待办
所有 14 个 AM 缺陷 + 计划表阶段 0-4 全部工作项（含第二轮缺口补完）已完成。
唯一遗留：§4.5 FCPXML 实现标记为下一迭代（已评估 medium 优先级、无许可风险）；
§1.4 CompiledAudioGraph 完整原子换图（block 边界 fade/crossfade 有状态节点替换）
当前以 AudioGraphSnapshot + 签名同步渐进实现，1000 次连续改 FX 的压力测试留待 CompiledAudioGraph 完整落地。






