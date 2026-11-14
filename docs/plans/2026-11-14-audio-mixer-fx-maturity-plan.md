# Audio Mixer 与五款效果器成熟化实施计划

- 日期：2026-11-14。
- 范围：Audio Mixer、EQ、Dynamics、Panner、Delay、Reverb；普通轨、Bus、Master、实时播放、导出、工程状态、遥测和窗口生命周期。
- 目标：采用业界成熟、公开可实现的算法和交互契约，获得稳定、可解释、可测试的专业音频工具。
- 非目标：逐样本复制 DaVinci Resolve 专有算法或保证其模拟音色完全一致；不为追求外观一致掩盖能力缺口。
- 执行状态：本文是后续后端实施计划。此次审计没有实现本计划中的后端修复。主 Mixer UI／交互改进单独记录于第 3 节，不能视为后端验收完成。
- 代码基线：审计围绕提交 6fecfb1c 及本次工作区。后续 UI 变更可能移动行号，以下函数名和文件是定位依据。
- 原则：遵循项目 .clinerules，复用 model、command/service、Effect Catalog/Registry、现有音频后端和导出路径；不另建脱离工程状态的 UI 音频播放器。

## 1. 已完成验证与结论边界

### 1.1 真实播放结果

使用本项目 [2K_h264.mp4](/Users/heng/Documents/vscode/topos-studio/tests/test_videos/2K_h264.mp4)，在完整主窗口中新建普通 A1 音轨并挂载五款效果器：

- 真实 AudioSink 播放约 12 秒，收到 211 条遥测载荷。
- EQ 收到 96 个频谱 bin。
- Dynamics、Reverb、Delay 电平发生变化，观测区间约 -39.74 至 -0.158 dB。
- 用户已确认面板能够动起来。因此不能继续把“当前普通音轨播放全断链”作为已复现根因。
- 停止后存在部分效果器读数残留；这与“播放时没有读数”是不同问题，需分别回归。
- Panner 舞台、Delay 回声图、Reverb 示意图主要表达参数／结构，并不要求像示波器一样持续改变几何形状。是否运行应由真实 tap、处理结果和播放状态共同证明。

当前已有 15 项遥测相关测试与另批 145 项相关测试通过。通过的测试证明已有覆盖范围没有失败，不能证明本文新增缺陷不存在，也不能代替完整主窗口、真实音频输出和导出验证。

### 1.2 缺陷证据等级

| 标记 | 含义 | 使用要求 |
|---|---|---|
| 确认缺陷 | 源码路径明确，且有数值／Qt 对象／真实运行复现，或契约矛盾可直接验证 | 必须修复并补能暴露原问题的 focused test |
| 确认限制 | 代码明确只实现了部分能力，例如 stereo Panner | 实现完整能力，或让 UI、能力声明和交付诊断准确表达限制 |
| 待测对齐 | 有成熟方案或手册语义，但需要实测确定映射、默认值或听感目标 | 先建立参考，再编码；不可把猜测写成已确认 bug |
| 可选增强 | 超出本次五款对应功能，或非当前产品必要能力 | 单独排期，不能阻塞基础正确性，也不能冒充 Resolve 原功能 |

P0：可能造成错误声音、效果丢失、成片不一致或状态不可恢复。P1：核心专业能力和稳定性。P2：高级工作流或增强。优先级代表本项目实施顺序，不等同于安全事件等级。

### 1.3 已数值复现的关键问题

| 问题 | 当前结果 | 预期／判据 |
|---|---|---|
| EQ HPF slope 6/12/18/24 | 实测远端斜率约 12/24/36/48 dB/oct；截止处约 -3/-6/-9/-12 dB | 标称斜率与实际传输一致，UI 与 DSP 同源 |
| Shelf Q 0.3→10.3 | 随机音频输出最大差 0，UI 曲线却变化 | 有效 Q 控制真实滤波器，或明确采用不提供 Q 的 Shelf |
| Reverb Brightness 50→100 | 两实例处理整秒随机音频，输出最大差 0 | 运行中阻尼随参数改变，不清空原尾音 |
| Panner 矩阵计算 | 幅度约 0.4 的输入与正确矩阵乘法最大差 0.1465 | 两输出都由未污染的原输入计算 |
| Expander threshold=-20、input=-40、ratio=2 | 额外衰减仅 10 dB | 标准 1:2 下扩额外衰减 20 dB，受 Range 封顶 |
| Sidechain Listen | ±0.5 检源变为全正，示例输出 DC=0.375 | 监听实际检源音频，不播放整流检测量 |
| Dynamics 总开关 | 模块初始 [False,True,False]，关→开后全 True | 保留每模块独立旁路组合 |
| Flat Bypass 预设 | 总电源开时应用后三模块全 True | 预设／Reset／总旁路各有独立、清晰语义 |
| Delay Link 开一次 | 实际发出 2 条 committed | 一个离散手势最多一次工程提交 |
| 片段结束后的 Delay 尾音 | 2048 点脉冲、60ms、100% wet；后块 decoder=None 时 peak=0 | 显式喂零同状态后块 peak=0.29427，说明尾音被跳过 |

上表是本项目自身数值正确性证据，不意味着已经完成与 Resolve 的声学 A/B 或专有算法比对。

## 2. 来源、能力口径与工程边界

### 2.1 官方参考

本机官方文档：[DaVinci Resolve Manual.pdf](</Applications/DaVinci Resolve/DaVinci Resolve Manual.pdf>)，Resolve 20；按 PDF 页面编号查阅：

| 页码 | 本计划使用的事实 |
|---|---|
| 3855、3859–3860 | Mixer 分阶段处理、插入效果顺序、专用 Dynamics/EQ、效果链复制和通道预设 |
| 3861–3866 | 三模块 Dynamics、独立模块开关、总旁路、处理后的 Make Up、侧链 Source/Listen、Mix 方向、时间参数 |
| 3866–3869 | 六段 EQ、滤波形状、6–24 dB/oct、可调响应和整体输出增益 |
| 3873–3878 | 2D／3D 声场、输出布局、Front/Back／Height、Spread／Divergence、Boom/LFE |
| 3950–3951 | Delay 的双线、正负反馈、输入选择、反馈阻尼、交叉反馈、干湿和输出 |
| 3971–3973 | Reverb 的 Direct／ER／Late、房间几何、传播时间、Distance、Brightness、Modulation和独立音色 |

事实口径：

- 本计划采用成熟开放算法，追求参数语义、声音正确性和工程工作流成熟，不承诺 Earth/Air/Ice/Fire 等专有响应的精确复刻。
- 原 Delay 的输入选择包含 Stereo、Mono、Left、Right；不能把当前菜单中的 LCR/Quad/5.1 等字符串当成已经实现的真实多声道 Delay。
- Reverb 负 Pre Delay 用于抵消传播时间；不能直接把整个负区间钳成零。
- 官方 Dynamics Mix 的数值方向与当前 Topos 不同，应明确产品契约并提供迁移，而不是悄悄反转已有工程。
- BPM 同步、卷积混响、True Peak/过采样、Ambisonics／Atmos 全流程是独立能力；不把它们一概认定为这五款基础面板的必备项。

### 2.2 成熟技术来源

以下仅概括方法，不复制第三方实现。所有引入代码、依赖和原生库须另外检查许可证及平台支持。

- [W3C Audio EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/)：用 RBJ biquad 作为 Bell／Shelf／Notch 的公开系数来源；注意数字域频率和带宽定义。
- [AES：Digital Dynamic Range Compressor Design—A Tutorial and Analysis](https://aes.org/publications/elibrary/elibrary-page/?id=16354)：以检测器、增益计算和时间平滑分层设计动态处理。
- [Aalto VBAP library](https://research.spa.aalto.fi/projects/vbap-lib/vbap.html)：多扬声器幅度声像使用二维扬声器对／三维三元组与能量归一。
- [Julius O. Smith：FDN Reverberation](https://dsprelated.com/freebooks/pasp/FDN_Reverberation.html)：晚期混响可使用交叉耦合延迟线和正交反馈矩阵；不把孤立并联 comb 自动等同于完整 FDN。
- 本计划代码骨架为独立解释示例。示例表达状态和数学契约，不能直接把逐采样 Python 循环放进实时音频线程。

### 2.3 不得破坏的项目约束

1. 所有工程变更走不可变模型和命令入口，拖动期 preview、结束 committed；支持 undo/redo、revision、保存和重开。
2. UI 不运行解码和重 DSP；不在 UI／普通脚本线程调用 OpenGL/ModernGL。
3. 音频后端公开参数 schema、版本和 preview/export 能力；不支持组合明确诊断，禁止静默丢弃效果。
4. DSP 状态按 track/instance 隔离；后台控制消息在块边界应用；实时线程不等待磁盘、网络或 UI。
5. 新 schema 保留未知字段和其他域 payload，提供旧工程／旧预设迁移；工程不保存机器绝对路径。
6. 修改前先分析调用者，修改后独立审查。原有用户工作区改动不纳入本计划的重构范围。
7. 所有数值输入验证 finite、范围、维度、采样率；故障日志能定位 track_id、instance_id、阶段与原因。

## 3. 本次主 Mixer UI／交互交付

本节与后端待办分开管理。主 Mixer 外观和交互可先改善，但不能因此把尚未实现的后端功能显示为已可用。

### 3.1 本次 UI 工作的契约

- 保持现有 command/service 与信号接口，复用五款效果器现有设计语言。
- 改善条带层级、选中／hover／旁路／静音／独奏／录音状态、效果列表、推子／声像反馈和主输出辨识。
- 控件含义、单位、tooltip、禁用原因和错误态一致；不能制造与后端无关的随机示波／假电平。
- 右键、拖动、双击复位、数值输入、键盘焦点和快捷键不相互冲突。
- 小窗口、窄条带、高 DPI、中英文界面保持可操作，不遮挡读数。
- 缺失的 Master FX、多声道 DSP、录音硬件等能力依现有能力门控处理；不能仅添加可点击按钮。
- 新动作若需要模型支持而后端未实现，记录为后续依赖，不创建只修改 UI 的伪实现。

### 3.2 实际交付记录

| 交付项 | 本次实现 | 边界 |
|---|---|---|
| 通道宽度 | 标准112px，能完整显示Parametric EQ；紧凑70px | 长于可用空间的名称仍省略，tooltip显示全名 |
| 纵向对齐 | 固定tag／轨名／badge／rack区高度；标准三行FX、紧凑两行FX | 超过行数内部滚动，不挤短单条轨道的推子 |
| Master与滚动 | Master固定在右侧；横向滚动区和Master预留同高footer | 横向滚动出现／消失不改变普通轨与Master的行程关系 |
| 视觉层级 | Topos青色强调、低对比条带背景、顶部轨道色细线、M/S/R分色反馈 | 保留自有视觉，不照搬Resolve像素和美术资产 |
| 效果器列表 | 始终可见的+；active/bypass状态点；选中身份和滚动位置尽量保留 | 沿用原实例id和效果链命令 |
| 效果器快捷键 | 列表聚焦时Enter编辑、Space旁路、Delete/Backspace移除 | 仅作用于该列表焦点，不注册全局音频快捷键 |
| Pan区域 | 三行压成两行；展开按钮与数值放同一行 | 不新增空间DSP或改变pan数值映射 |
| 视图工具条 | Inserts显隐、Compact切换、可见轨数、更多菜单恢复隐藏轨 | 只改变视图，Inserts隐藏不会旁路DSP；新轨继承视图模式 |
| 编辑与反馈 | 轨名省略／完整提示；数值键入完成再提交；静音时仍能预设gain/pan | M继续控制静音；沿用preview/committed接口 |
| 缩放读数 | 电平表resize后按当前dB重算像素位置 | 不等待下一音频包，不修改音频测量算法 |
| 删除／撤销重建 | 隐藏轨以同id重新注册时保持隐藏偏好与计数一致 | 不更改工程音频状态 |
| 语言 | 新增中英文字段、tooltip和无障碍名称 | 其他语言沿用既有英文回退 |

修改文件：E05、E06、[audio_level_meter.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/audio_level_meter.py)、共享en_US/zh_CN语言文件；新增[UI回归测试](/Users/heng/Documents/vscode/topos-studio/tests/shared/test_audio_mixer_compact_ui.py)。AudioFader数值映射、拖动／滚轮合成会话、DSP、音频播放／导出及工程schema均未修改。

独立Review发现并已修复两项UI问题：横向滚动条仅占普通轨高度造成Master行程错位；隐藏轨删除后重建时实际显示与隐藏集合不一致。回归包含8轨／400px、2轨／250px、8轨紧凑／300px、多FX／空FX／Master对齐等边界。

验证与产物见[本次证据说明](/Users/heng/Documents/vscode/topos-studio/docs/audit/2026-11-14-audio-mixer/README.md)。第1节的12秒真实主窗播放是在后端审计基线上完成；本次UI版本另行做布局渲染、信号和交互回归，不把静态布局预览称作新的音频实播证据。后端缺陷、Master FX、尾音、预设保存等均仍为待实施，不因UI优化而标记完成。

## 4. 代码证据索引

以下路径已确认存在。行号以审计时版本为定位提示，函数名是后续核对依据。

| 编号 | 文件／入口 | 证据 |
|---|---|---|
| E01 | [audio_dsp.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_dsp.py) | StripEQFilter、LimiterEffect、CompressorEffect、ExpanderEffect、ReverbEffect、DelayEffect、SpatialPanner、EffectChain |
| E02 | [audio_playback.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_playback.py) | AudioMixer.mix_samples、AudioPlaybackThread、collect_spectra、_emit_silent_levels、reset_dsp_states |
| E03 | [audio_graph_renderer.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/audio_graph_renderer.py) | 离线共享节点、render_track_block、render_bus_stage、apply_master |
| E04 | [timeline_export.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_export.py) | 音频导出按 clip／track 遍历、4498附近调用 render_track_block 未传 sidechain_taps |
| E05 | [audio_mixer_panel.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/audio_mixer_panel.py) | update_per_track_levels、update_master_level、deregister_track、_setup_master_connections、频谱门控 |
| E06 | [track_mixer_widget.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/track_mixer_widget.py) | update_track_telemetry、reset_meter、对话框字典、DSP参数编辑提交 |
| E07 | [strip_eq_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/strip_eq_dialog.py) | 图形响应计算、Shelf Q、频谱和采样率入口 |
| E08 | [dynamics_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/dynamics_dialog.py) | _on_power_toggled、_on_preset_selected、reset_dynamics、Make Up、update_meters |
| E09 | [panner_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/panner_dialog.py) | 2D/3D舞台、参数回填、格式选择、预设 |
| E10 | [delay_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/delay_dialog.py) | _on_link_toggled、_switch_bank、_save_new_preset、输出表 |
| E11 | [reverb_dialog.py](/Users/heng/Documents/vscode/topos-studio/src/shared/ui/audio_mixer/reverb_dialog.py) | 几何／IR示意图、_switch_bank、_save_new_preset、输出表 |
| E12 | [timeline_v2.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_v2.py) | 参数 dataclass、TrackEffectInstance、效果链和轨道状态 |
| E13 | [mixer_preset.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/mixer_preset.py) | ChannelStripPreset、MixSnapshot、用户预设存储 |
| E14 | [audio_commands.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/commands/audio_commands.py) | ApplyChannelStripPresetCommand、ApplyMixSnapshotCommand、效果参数命令 |
| E15 | [edit_page.py](/Users/heng/Documents/vscode/topos-studio/src/features/edit/ui/edit_page.py) | manager与Mixer连接、音轨同步、播放状态 |
| E16 | [edit_controller.py](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/edit_controller.py) | 应用级Mixer／timeline状态同步 |
| E17 | [遥测测试](/Users/heng/Documents/vscode/topos-studio/tests/shared/test_cw_a1_gating_and_telemetry.py) | 已有频谱门控／peak-only dispatch等回归 |
| E18 | [Mixer单元测试](/Users/heng/Documents/vscode/topos-studio/tests/shared/test_audio_mixer_units.py) | 已有UI／数值单元覆盖 |

### 4.1 实施计划总表

状态更新（2026-11-14）：逐项实施状态见[实施证据文档](./2026-11-14-audio-mixer-fx-maturity-evidence.md)——P0 已全部实施并有 focused 回归；P1 部分交付（边界注明）；MX-03/10/14/17/18 与 PAN-02/03、RV-02/03、EQ-02/03 属阶段 F–H 待办。按阶段依赖推进，避免只按表格顺序一次性改完。

| ID | 优先级 | 工作项 | 主要前置／阶段 | 状态 |
|---|---|---|---|---|
| MX-01 | P0 | 停止／暂停／缺轨统一清表 | A→B | 已实现（2026-11-14，见证据文档） |
| MX-02 | P1 | 每实例真实输入输出／GR／SC tap | B→F | 已实现（2026-11-14，见证据文档） |
| MX-03 | P1 | Master／Bus处理与遥测闭环 | D、E→F | 待实施 |
| MX-04 | P0 | 同轨clip先求和、轨DSP一次处理 | B、C→D | 已实现（2026-11-14，见证据文档） |
| MX-05 | P1 | 处理顺序、send／tap位置建模 | D | 核心已实现（边界见证据文档） |
| MX-06 | P0 | 按instance_id复用状态、旁路连续 | B | 已实现（2026-11-14，见证据文档） |
| MX-07 | P0 | 尾音、延迟补偿、参数平滑 | D | 已实现（2026-11-14，见证据文档） |
| MX-08 | P0 | 外部侧链预览／导出同构 | MX-04→D | 已实现（2026-11-14，见证据文档） |
| MX-09 | P1 | FFT环窗、立体声能量、采样率 | G | 已实现（2026-11-14，见证据文档） |
| MX-10 | P1 | 效果参数自动化与时间戳控制 | D→G | 待实施 |
| MX-11 | P0 | 完整预设／快照apply与undo | B、D→E | 已实现（2026-11-14，见证据文档） |
| MX-12 | P1 | 窗口／订阅清理与错误诊断 | B | 已实现（2026-11-14，见证据文档） |
| MX-13 | P1 | 推子硬零、headroom、单位／增益定律 | F | 已实现（2026-11-14，见证据文档） |
| MX-14 | P1 | 前后级Send、独立pan／mute／自动化 | MX-05→F | 待实施 |
| MX-15 | P1 | 声道格式、Patch与降混 | F | 核心已实现（边界见证据文档） |
| MX-16 | P1/P2 | 表计校准、监听与可选True Peak | G | 核心已实现（边界见证据文档） |
| MX-17 | P2 | Group／Link／VCA控制工作流 | I后可选 | 待盘点／实施 |
| MX-18 | P1/P2 | 录音／独奏／设备生命周期核验 | G | 待验证／补齐 |
| EQ-01 | P0 | 正确滤波斜率、Shelf Q、同源响应图 | C | 已实现（2026-11-14，见证据文档） |
| EQ-02 | P1 | Solo／Voicing与六段能力契约 | C后 | 待实施 |
| EQ-03 | P1 | 分析仪／peak hold／reset | MX-02/09 | 待实施 |
| DY-01 | P0 | 总旁路、预设、Mix、Make Up | B、C | 已实现（2026-11-14，见证据文档） |
| DY-02 | P0 | 标准压缩／下扩／Gate gain computer | C | 已实现（2026-11-14，见证据文档） |
| DY-03 | P1 | Attack／Hold／Release、Limiter校准 | G | 核心已实现（边界见证据文档） |
| DY-04 | P0 | 原始检源Listen与侧链遥测 | MX-08→C/D | 已实现（2026-11-14，见证据文档） |
| PAN-01 | P0 | 无别名矩阵、单一声像增益定律 | C | 已实现（2026-11-14，见证据文档） |
| PAN-02 | P1 | 多扬声器VBAP／高度／Spread | MX-15→H | 待实施 |
| PAN-03 | P1/P2 | 真LFE、位置自动化、高级模式 | F、G→H | 待实施 |
| DL-01 | P1 | Stereo／Mono／Left／Right、Link单提交 | B、G | 已实现（2026-11-14，见证据文档） |
| DL-02 | P1 | 分数延迟、读头平滑、稳定反馈 | G | 已实现（2026-11-14，见证据文档） |
| DL-03 | P0 | 真实尾音、参数图与实例电平 | MX-07→D | 已实现（2026-11-14，见证据文档） |
| RV-01 | P0 | Brightness就地阻尼更新 | C | 已实现（2026-11-14，见证据文档） |
| RV-02 | P1 | 几何ER、正负Pre Delay、Distance | G | 核心已实现（边界见证据文档） |
| RV-03 | P1 | 校准Schroeder／FDN、RT60／调制 | H | 待实施 |
| RV-04 | P1 | 网络热更新、三路输出与尾音 | D | 已实现（2026-11-14，见证据文档） |
| ST-01 | P1 | 用户效果预设原子持久化 | E | 待实施 |
| ST-02 | P1 | A/B和外部回填一致性 | E | 核心已实现（边界见证据文档） |
| ST-03 | P0 | schema／算法版本、兼容、能力声明 | A起草，E完成 | 核心已实现（边界见证据文档） |

## 5. Mixer 后端与工作流任务

### MX-01：停止、暂停、无输入时的遥测生命周期

- **状态／优先级：确认缺陷，P0。证据：E02._emit_silent_levels、E05.update_per_track_levels、E06.reset_meter。**
- **现状：** stop/pause广播旧格式 list，面板只更新strip，效果器dict遥测入口不执行；缺席轨道同样只清strip，窗口最后一帧残留。
- **实施：** 定义统一版本化payload，包含播放代数、时间戳、valid/silent、pre/post、实例读数和采样率；兼容旧list时转换为完整静音事件；向所有可见消费者广播停止，清GR、SC、当前频谱和历史轨迹，峰值保持按明确定义处理。
- **步骤：** 先写停止残留回归；增加适配器；绑定播放代数丢弃旧排队事件；统一暂停／停止／seek／缺席轨；最后移除旧格式生产路径。
- **验收：** 停止后最多2个UI更新周期内所有当前电平与GR归零；重新播放无旧值闪回；静音轨、删除clip、solo剔除轨与空payload行为一致；不靠QTimer生成假信号。

### MX-02：每实例、每阶段真实读数

- **状态／优先级：确认限制，P1。证据：E06.update_track_telemetry把同一末端peak分发给EQ、Dynamics、Reverb、Delay。**
- **实施：** tap键使用track_id／instance_id／stage，支持input/output、SC、GR、频谱；UI可明确选择pre/post，避免把轨末端冒充某个插件输出。
- **步骤：** 先定义tap DTO与消费者订阅；在统一图阶段采样；仅开放可见消费者需要的高成本FFT；UI用queued/coalesced更新，显示数据无效／后端不可用状态。
- **验收：** 两个Delay之间放置-12dB增益，前后表显示可预期差值；同类型多实例不串读数；旁路GR为0，消费者关闭后停止对应FFT；UI刷新目标约30Hz，不随视频帧率改变。

### MX-03：Master 与 Bus 完整处理能力

- **状态／优先级：确认限制，P1。证据：E05._setup_master_connections/update_master_level/频谱门控；E02 Bus阶段只构造peak/rms。**
- **实施：** Master参数、FX、EQ／Dynamics／Panner能力走专门模型和命令；Bus补pre/gr/sc/spectrum及真实实例tap；Master纳入订阅和FFT门控。
- **步骤：** 先补能力声明和不可用诊断；再定义Master图节点及保存／导出契约；连接编辑信号；最后开放对应控件。不得只给Master窗口塞普通轨读数。
- **验收：** 同参数在普通轨、Bus、Master可解释地生效；嵌套Bus和Master输出读数不同；保存重开／undo／导出一致；未实现前入口不能宣称生效。

### MX-04：轨输入先求和、轨DSP只推进一次

- **状态／优先级：确认缺陷，P0。证据：E02.mix_samples在decoder循环内执行pan/EQ/track FX；E04也按clip处理共享轨节点。**
- **实施：** clip decode／fade／clip gain／clip FX先按时间位置汇入轨输入块；随后轨Trim／FX／Dynamics／EQ／Panner／fader各运行一次；实际顺序由MX-05模型控制。
- **步骤：** 构建有两重叠clip的最小图；提取共享轨节点处理；实时与离线接同一block scheduler；保留clip级和track级不同尾音／延迟域。
- **验收：** 两decoder同轨的结果等于先逐样本求和后运行同一轨链；互换decoder遍历顺序不改变声音；压缩阈值检测合成信号，有状态Delay只推进一个块；非重叠两clip之间尾音连续。

### MX-05：显式处理顺序、tap与send位置

- **状态／优先级：确认限制／待测对齐，P1。证据：E02/E03固定post-fader路径；官方3855、3859。**
- **实施：** 模型声明clip/pre、trim/polarity、insert、Dynamics、EQ、Panner、fader、pre/post-send、Master阶段；公开支持的顺序组合和总insert旁路语义。
- **步骤：** 文档化当前顺序；建立带版本处理顺序枚举；统一preview/export编图；为旧工程保持原顺序，用户显式转换后采用新顺序。
- **验收：** EQ boost在Compressor前后产生可预测GR差异；pre-send不随fader改变、post-send跟随；总insert bypass不误旁路独立EQ/Dynamics；各阶段tap对应实际节点。

### MX-06：实例身份与旁路状态连续性

- **状态／优先级：确认缺陷，P0。证据：E01.EffectChain.set_params按class取首个旧实例，忽略instance_id；collect_gain_reduction未过滤旁路。**
- **实施：** 以稳定instance_id匹配状态，校验类型；legacy无id时分配一次并迁移；新增／删除／重排／替换分别处理；旁路读数清零，不暴露上一块GR。
- **步骤：** 双同类Delay产生不同尾音后调序；先失败测试；修复身份池；定义旁路保留／drain／reset策略及短ramp；删除释放缓存和订阅。
- **验收：** A/B交换顺序后各自仍携带自己的延迟线和参数；删除A不把其状态给B；两条轨同id域互不共享；旁路切换无过期GR或突然复活的旧位置尾音。

### MX-07：尾音调度、延迟补偿与参数平滑

- **状态／优先级：确认缺陷／能力限制，P0。证据：E02 decoder边界返回None后跳过；E04只遍历片段重叠区；E01网络热重建。**
- **实施：** 节点报告latency_samples、tail_active、tail_estimate、reset原因；无clip但有尾音时喂零；图汇合点补齐支路延迟；参数在块内按定义平滑。
- **步骤：** 先实现轨尾音和clip尾音契约；再实现零输入drain；规定停止／暂停／seek／自然结束区别；实现导出选区内尾音和允许扩展的显式交付策略；最后接lookahead／负时差等延迟节点。
- **验收：** 60ms脉冲示例后块有回声；Reverb尾音自然衰减；seek清除旧位置状态；延迟并行支路对齐≤1采样；真实输出和导出对齐；强反馈不会无限挂住。
- **边界：** 高feedback可能产生极长尾音。最大尾长必须是可见交付策略，不得无诊断截断；静音／solo是否切断尾音必须全图一致。

### MX-08：外部侧链在普通轨、Bus、导出同构

- **状态／优先级：确认缺陷，P0。证据：E04调用render_track_block未传sidechain_taps；E02/E03 Bus链未完整接外部检源。**
- **实施：** 区分音频路由DAG与检源依赖DAG；block-major调度，按依赖提供同时间窗tap；反馈侧链显式增加一个块或固定采样延迟并诊断。
- **步骤：** 先锁定Source tap位置和SC Listen；实现普通轨→普通轨、Bus→普通轨、普通轨→Bus、Bus→Bus；导出复用；缺失源、muted源、循环源有明确政策。
- **验收：** 100Hz节目＋脉冲检源，预览／导出GR包络一致；源track删除不静默误用其他源；反馈循环不死锁；不同块大小保持定义好的固定延迟，而不是意外一帧差。

### MX-09：频谱与采样率正确性

- **状态／优先级：确认缺陷／限制，P1。证据：E02._push_spectrum_window用mean折叠；collect_spectra未按write指针重排；E07.update_sample_rate无完整下发链。**
- **实施：** 按环窗write位置重排后加窗；FFT按L/R功率合成或分别显示，另设明确Mid模式；校准窗增益、峰值／功率单位与对数bin聚合；采样率由引擎下发。
- **步骤：** 抽取纯分析服务；增加反相信号、环绕、变采样率测试；缓存窗、频率映射与工作数组；同步清空和关闭订阅。
- **验收：** L=-R仍显示双声道能量，Mid模式才允许相消；1kHz频点在44.1/48/96kHz定位正确；环窗跨界无伪瞬态；不同块分割结果容差明确；音频输出不受FFT失败影响但有诊断。

### MX-10：自动化与控制消息

- **状态／优先级：确认限制，P1。证据：E02/E03轨级自动化主要消费volume/pan/mute；效果参数动画未形成统一轨参数契约。**
- **实施：** 参数路径使用instance_id和稳定schema键，补可自动化标志、单位、插值域、范围；control线程准备变更，音频线程在时间戳对应样本应用。
- **步骤：** 先gain/threshold/dry_wet连续参数，再Delay时间／Reverb几何等需特殊平滑参数；离散开关使用事件；record/touch/latch复用现有自动化体系。
- **验收：** 24/29.97/59.94fps及不同音频块长得到相同参数时间表；预览／导出误差≤约定浮点容差；undo恢复曲线，不只恢复最后UI值。

### MX-11：预设、快照与工程恢复

- **状态／优先级：确认缺陷，P0。证据：E13可捕获FX/routing但缺panner；E14.ApplyChannelStripPresetCommand和ApplyMixSnapshotCommand仍主要恢复volume/pan/mute/solo/EQ。**
- **实施：** 建立单一ChannelStripState DTO，capture/serialize/apply/undo覆盖同一字段；包含FX、instance身份、Panner、routing、send、处理顺序和必要Master字段。
- **步骤：** 先列字段覆盖表；升级schema与迁移；一次原子命令应用；跨轨复制生成新实例身份并正确映射侧链／Bus；unknown字段保留。
- **验收：** 完整快照A→B→A逐字段一致；重开后FX和routing仍在；undo恢复所有字段；目标Bus缺失明确提示；不再出现“预设文件存了效果但应用没恢复”。

### MX-12：窗口、订阅与异常诊断

- **状态／优先级：确认缺陷／限制，P1。证据：E05.deregister_track、E06对话框parent=self.window及字典清理；遥测dispatch有宽泛吞错路径。**
- **实施：** track移除／实例删除／工程切换时关闭对应独立窗口，清理引用、门控和订阅；对话框用generation/identity校验迟到事件；窄范围捕获已知异常并限频记录。
- **步骤：** 统一effects-window registry；绑定finished/destroyed和model生命周期；meter DTO验证后再进入widgets；可选API故障不打断兄弟窗口。
- **验收：** 删除轨后无孤儿窗／FFT；打开关闭100次订阅不累积；已删除实例迟到编辑不复活效果；缺少可选字段只降级该表；后台错误能定位且不刷屏。

### MX-13：推子零点、增益余量与单位

- **状态／优先级：确认限制／待测校准，P1。证据：E06.db_to_linear(-60)=0.001，而模型允许volume=0；Panner与主输出另有增益处理。**
- **实施：** 明确推子底端是-60dB还是-∞；建议把硬零作为独立端点，同时保留连续dB有效区。区分通道增益dB、音频峰值dBFS、响度LUFS，不把推子0dB与输入0dBFS当作同一量。
- **步骤：** 先冻结旧工程linear↔dB映射；新增zero标记或端点策略；校准mono等功率pan与stereo balance；记录内部浮点headroom和Master保护器行为，不能用隐藏限幅掩盖每级削波。
- **验收：** volume=0保存／重开／undo后严格为0；最小推子和数值输入按选定契约一致；0dB增益直通；+6dB左右约两倍振幅，在保护前测量；声像中心和极端位置有独立数值oracle。

### MX-14：Send的真实前后级与独立控制

- **状态／优先级：确认限制，P1。证据：E12.Send只含target_bus_id、level、enabled并明确post-fader；routing_dialog只编辑这些字段。**
- **实施：** 在版本化Send模型增加tap位置、声像／声道映射、独立mute和自动化地址；保留当前post-fader为旧工程默认。Bus输出路由与辅助Send是两条独立关系，不能相互替代。
- **步骤：** 定义pre/post-fader以及pre/post-FX的合法tap集合；在统一图上生成Send节点；补命令、保存、快照、自动化、导出；最后再在Routing交互中开放新参数。
- **验收：** 通道推子降低12dB时post-send随之变化，pre-send不变；Send mute不影响主输出；删除目标Bus提供可恢复诊断；共享100%wet Reverb Bus不重复混入干声；多级Bus/send循环可检测且不挂起。

### MX-15：声道格式、输入输出Patch与降混

- **状态／优先级：确认限制／待全链路核验，P1。证据：已有ChannelLayout/adapt_channels和素材选道，Panner仍2×2；菜单声道名不等于整链支持。**
- **实施：** 建立source→clip→track→bus→device/export的布局能力矩阵，明确Mono/Stereo/5.1/7.1/高度声道的顺序、上混／降混矩阵和LFE处理。优先复用已有ChannelLayout和SourcePatch，不再创建另一套按声道数猜布局的映射。
- **步骤：** 枚举现有支持并逐段标记；用每声道独特脉冲验证；把布局纳入graph编译与effect capability；跨布局路由显式适配；设备不足时允许离线导出验证并说明监听降混。
- **验收：** 每个目标声道编号／语义正确，无交换Center与LFE；只选C轨或5.1(side)等具体布局不退化成盲目截前两路；未知布局不静默丢声道；预览与导出使用同一转换矩阵。

### MX-16：专业表计与监听的可信度

- **状态／优先级：已有能力的待测校准，P1；True Peak和扩展监听为可选增强P2。证据：已有Peak/RMS/LUFS与后端探测，但不能以菜单存在证明校准达标。**
- **实施：** 标明各读数测量窗、参考值和单位；持续量与峰值保持分开；clipping计数／复位、相关度与mono兼容检查按需要提供。True Peak若实现，明确过采样滤波与单位dBTP，不把sample peak重新命名为True Peak。
- **步骤：** 用独立参考WAV和测量器校准Peak/RMS/LUFS；验证立体声合并权重、静音门限、积分重置；监听mute/dim/solo与渲染导出分开建模；UI只能展示真实可测值。
- **验收：** 满幅正弦sample peak约0dBFS、未偏置RMS约-3.01dBFS；LUFS对照所选公开参考文件并记录容差；停止后当前值和hold策略符合MX-01；mono折叠暴露反相问题；监听dim不降低成片导出电平。

### MX-17：分组／VCA与批量工作流（可选）

- **状态／优先级：能力盘点／可选增强，P2。当前未建立完整VCA状态与控制契约，本次不假定已有分组服务不存在。**
- **实施：** 先复核现有项目分组／选择服务，区分编辑Group、控制Link、音频Bus和VCA。VCA只施加控制增益，不新增一次音频求和；批量调整保留成员相对值。
- **步骤：** 定义成员身份和增益叠加；检查循环／嵌套限制；一次宏命令提交；复用自动化和快照；仅在核心Mixer正确后开放。剪辑软件默认界面不必放出完整DAW控制面板。
- **验收：** VCA推子-6dB时成员相对差保持；Bus与VCA同时使用不会双倍衰减；任一成员处于硬零时保持硬零；解除分组、删除成员、undo和重开不丢原值。

### MX-18：录音、监听、独奏与设备生命周期

- **状态／优先级：已有功能的端到端验证，P1。Record Arm能力开关当前为True且已有RecordingManager，不能称“尚未实现录音”。**
- **实施：** 核验R按钮→输入Patch→arm／monitor→录音take→素材导入的完整路径；区分输入监听、录音和已录片段播放。补独奏可听闭包、solo-safe／PFL/AFL的产品契约，其中未选择实现的高级监听作为P2保留。
- **步骤：** 使用真实可用输入设备；先模拟设备消失／写盘失败，再测录音；校准输入输出延迟、pre-roll和BWF时间戳；将硬件输入／控制室监听与离线工程导出能力分开声明。
- **验收：** arm状态不等于正在录音；停止／取消／设备拔出都能安全收尾并保留take；无意外反馈回授；solo普通轨不截断必经Bus；监控设备切换后工程gain/routing不被悄悄重置。

以上扩展覆盖Mixer常见的增益、路由、格式、表计、分组和录音域；可选增强先留计划，不给有限的默认界面增加占位按钮。

## 6. EQ 任务

### EQ-01：斜率、Shelf Q及图形响应统一

- **状态／优先级：确认缺陷，P0。证据：E01 StripEQFilter._band_sections约895–909；E07._band_magnitude_db。**
- **实施：** HPF/LPF按N=slope/6构造真正N阶Butterworth，odd-order保留一阶段；Bell/Notch用RBJ；Shelf定义有效Q或S参数并实际消费；UI直接计算同一SOS的频响。
- **步骤：** 先抽取无Qt的系数／响应服务；加6/12/18/24斜率测试；替换DSP和图；迁移旧工程的声音版本或提供明确转换；在音频块边界无突变替换系数。
- **验收：** 标称截止处Butterworth约-3.01dB；远离截止且远低于Nyquist的区间斜率误差≤0.5dB/oct；DSP冲激FFT与图差≤0.1dB；Shelf Q有效或UI不承诺该参数。

### EQ-02：六段布局、Solo与Voicing能力

- **状态／优先级：确认限制／待测对齐，P1。证据：E01.VOICING_Q_SCALE只乘Q；E07图与DSP Solo可能采用不同多Solo选择。**
- **实施：** 固定band身份，明确允许形状与频率／Q／增益范围；Solo定义为试听路径或只应用选段，UI和DSP一致；Voicing采用可说明的公开响应模型。
- **步骤：** 记录当前Clean/British/Warm/Punch为自有模型；不要把改名当成专有模拟实现；实现可测的带宽／增益耦合等目标；补单／多Solo和旁路规则。
- **验收：** 每一可用控件改变实际频响；所有可用形状和高低采样率finite；0dB Bell直通；多Solo行为一致；无未经测试的模拟音色等价声明。

### EQ-03：分析仪、峰值保持与重置

- **状态／优先级：确认限制，P1。证据：E06/E07当前轨末端频谱与输入／输出标识不足。**
- **实施：** 使用MX-02/09实例tap；SPEC/PEAK开关只改变显示／分析策略，不能改音频；定义peak hold时长、release和清除动作。
- **步骤：** 先统一单位／频率轴；接采样率、播放状态和峰值重置；可选pre/post双曲线留能力开关。
- **验收：** 纯音、噪声、静音、反相立体声符合分析定义；停止不保留活动频谱；增益改变后分析与实际output位置一致。

## 7. Dynamics 任务

### DY-01：总旁路、预设、Reset、Make Up和Mix契约

- **状态／优先级：确认缺陷／官方语义差异，P0。证据：E08约1247、1277、1310、1344；E01 CompressorEffect.process；官方3862–3866。**
- **实施：** 总旁路与三模块开关独立；预设保留自己的模块状态；Reset依产品默认值操作；Make Up作为post-dynamics节点，而非只附着Comp；清晰定义Mix方向。
- **步骤：** 用[False,True,False]补状态测试；复用chain-level与params-level语义或版本化复合单元；为旧Mix=wet百分比／旧MakeUp位置保留迁移；UI、DSP、导出和图同步。
- **验收：** 总开关往返不改模块组合；Flat Bypass真实旁路；Comp关闭时post-dynamics MakeUp仍按定义生效；若采用官方方向，Mix=0为全压缩、100为全原声；旧工程不得悄悄反向。

### DY-02：成熟gain computer与Gate状态机

- **状态／优先级：确认缺陷／待测校准，P0。证据：E01.ExpanderEffect._compute_gain_db约2659；Gate当前用高ratio近似。**
- **实施：** Compressor标准hard/soft knee；Expander使用ratio-1下扩斜率；Gate独立开门／hold／release／closed状态与迟滞，range限制最大衰减。
- **步骤：** 先纯静态transfer，后加检测器和平滑；把range/ratio/threshold单位和边界写schema；Graph调用相同transfer函数。
- **验收：** threshold=-20、ratio=4、input=-8时hard-knee压缩gain=-9dB；Expander input=-40、ratio=2额外衰减20dB，range=12时封顶12dB；Gate关闭达到范围，不在门限附近反复抖动。

### DY-03：Attack/Hold/Release、Limiter和动态范围

- **状态／优先级：确认限制／待测对齐，P1。证据：E01各模块平滑和hold路径；Limiter末端np.clip；E12参数范围与官方默认范围不完全一致。**
- **实施：** 明确时间常数、检测域和hold触发点；所有时间按采样推进；0ms有明确定义；Limiter分开gain smoothing和硬保护，报告过冲／失真。
- **步骤：** tone burst测响应；统一hold边界；以公开成熟检测器实现稳定包络；必要时优化非零hold路径；lookahead/true-peak独立可选，不当作基础面板必备。
- **验收：** Hold=100ms释放起点误差≤1采样；48/96kHz等时响应一致；整块/分块结果≤1e-5；limiter设置阈值时峰值与保护策略可验证；所有参数端点finite，无跨块增益跳回1。

### DY-04：真实Sidechain、Listen与模块遥测

- **状态／优先级：确认缺陷，P0。证据：E01约1421–1436播放abs检测量；E02/E04侧链缺口；E06模块读数路由。**
- **实施：** 检源audio与detector envelope分离；Listen保留波形极性；对source不可用／自身引用／循环引用诊断；每模块GR和输入输出tap独立。
- **步骤：** 依赖MX-02/08；定义声道合成和滤波位置；Source切换短ramp；实时、离线、Bus全部传同一检源。
- **验收：** ±0.5信号监听保持正负；1kHz正弦不产生整流DC；外部静音源不触发自身信号压缩；模块旁路GR为0；源删除不中断播放。

## 8. Panner 任务

### PAN-01：安全矩阵、增益定律与立体声语义

- **状态／优先级：确认缺陷，P0。证据：E01.SpatialPanner.process约3205–3209原地污染L视图；E02 balance后再次应用matrix。**
- **实施：** 两输出使用原输入或独立scratch矩阵乘法；明确balance、mono pan与stereo position的单一增益定律，避免意外双重声像衰减。
- **步骤：** 先以M@input建立oracle；修复alias；校准中心／极端位置和spread=0/100；定义功率归一；所有数组不改变调用方未授权输入。
- **验收：** 随机输入与矩阵oracle误差≤1e-6；左右对称输入镜像对称；中心unity／等功率依已选语义达标；不跨轨复用被污染scratch。

### PAN-02：真实输出布局与多扬声器声像

- **状态／优先级：确认限制，P1。证据：E01.SpatialPanner明确只有2×2；elevation/format/space_mode未参与真实多声道输出；官方3873–3878。**
- **实施：** stereo先稳定；再基于真实ChannelLayout和VBAP实现2D扬声器对、3D三元组；F/B、height、rotate、spread、divergence按布局生成增益，而非只动舞台。
- **步骤：** 定义2.0/5.1/7.1/7.1.4输出和设备能力；预计算有效基底；实现主方向与spread采样；UI与DSP共用坐标变换；降混明确处理。
- **验收：** 逐源冲激检查各目标扬声器能量；F/B能切换前后、height能进入上方声道；布局切换不串声道；无上方输出时明确限制；不把3D画面视作实际沉浸输出。

### PAN-03：Boom/LFE、自动化与高级模式边界

- **状态／优先级：确认限制／可选增强，P1/P2。证据：E01 Boom只是120Hz低频混回L/R；E02 pan自动化取块中点驱动matrix。**
- **实施：** LFE作为独立总线／声道，区分pre/post-fader和Only；立体声近似以准确名称声明；位置／spread等参数走MX-10；speaker snap、tilt、球坐标等按已选产品范围排期。
- **步骤：** 先真实LFE路由；然后逐样本或控制率插值声像；最后评估高级模式；Atmos/Ambisonics不因菜单存在而宣布支持。
- **验收：** Only只向LFE输出；低频主声道不因Boom意外重复加倍；pre不跟随fader、post跟随；轨迹跨块无阶梯爆音；图与实际方向一致。

## 9. Delay 任务

### DL-01：输入选择、Mono适配、Link与提交

- **状态／优先级：确认缺陷／限制，P1。证据：E10._on_link_toggled约903–914双提交；E01只识别Mono；E12菜单枚举与官方输入选择不同。**
- **实施：** Stereo/Mono/Left/Right明确选择矩阵；实际Mono轨显示单线控件且隐藏无意义Stereo Blend；Link双向一致，一手势一命令。
- **步骤：** 对照真实track layout做能力表；保留旧枚举的迁移／诊断；修双提交和左右控件回填；通过现有提交入口。
- **验收：** L-only/R-only冲激正确；Mono语义明确是sum还是average并标定电平；Link开关各一次committed；右侧拖动也能同步左侧；undo完整恢复。

### DL-02：连续延迟、负反馈、交叉反馈与阻尼

- **状态／优先级：确认缺陷风险／待测校准，P1。证据：E01约2116、2189改时间清零ring；整数delay；High Ratio使用自定映射。**
- **实施：** 最大长度预分配；fractional read；读头切换交叉淡化或明确定义磁带式变速；稳定反馈矩阵；阻尼按公开低通设计，High Ratio标定可解释映射。
- **步骤：** impulse oracle先测时间／符号；增加0ms和极短delay边界；分离容量变化与参数变化；高feedback下平滑限制变化。
- **验收：** 121ms时间误差≤1采样或选定插值精度；-feedback相邻回声交替极性；L/R交叉比例正确；拖动不清空整段历史；谱半径<1且最大feedback长跑finite、无爆音。

### DL-03：尾音与图形一致性

- **状态／优先级：确认缺陷／限制，P0。证据：MX-07；E10 tap_graph为参数图、输出表目前是轨末端。**
- **实施：** 依赖统一tail scheduler、实例tap和状态清理；参数回声图反映时间／反馈／极性，真实电平表示当前输出；不往图中加随机动画伪装音频输入。
- **步骤：** 片段尾和相邻片段混合回归；mono/stereo图适配；停播清读数；可选BPM同步单独任务。
- **验收：** 给定脉冲的回声位置与图一致；源结束后应有规定尾音；关闭窗口不停止实际效果；多个Delay读数能不同。

## 10. Reverb 任务

### RV-01：Brightness热更新和频率衰减

- **状态／优先级：确认缺陷／官方语义差异，P0。证据：E01约1613–1626只更新tone，实际comb阻尼在_apply_fdn_params；官方3972。**
- **实施：** 拆分RT60、阻尼、tone、几何更新；Brightness直接更新反馈阻尼且保留状态；最大brightness设计为各频段相同目标衰减，而非仍有明显频率相关衰减的固定20k低通。
- **步骤：** 补hot-update零差失败测试；修更新分类；测分频段EDC并校准brightness映射；tone不改变RT60本体语义。
- **验收：** 50→100时高频尾音能量有区别且不归零；最大brightness各测量频段T20/T30趋势一致；默认／边界值在48/96kHzfinite。

### RV-02：几何ER、传播时间与Distance

- **状态／优先级：确认近似／官方语义差异，P1。证据：E01._compute_taps只取H/L/W均值且先把pre_delay钳0；Distance也移动Late起点。**
- **实施：** 使用矩形房间一阶／有限阶image-source ER；各维独立决定反射路径、延迟、强度与方向；按有效传播时间处理正负Pre Delay；Distance按目标语义只改ER配置，避免无意移动所有Late。
- **步骤：** 明确虚拟source/listener默认位置；计算直接与反射路径；定义最大反射阶／能量阈值；IR图与DSP读同一tap描述；先参考测量再确定Pre Delay具体施加路径。
- **验收：** 相同平均尺寸不同形状产生合理不同ER分布；负predelay可抵消部分传播时延；不同distance的ER变化能解释；ER-only IR的首反射时间与图误差≤1采样。
- **候选公式：** effective_propagation=max(0, distance/343 + predelay_ms/1000)。这是因果实现骨架；反射附加路径、late启动和Direct是否补偿须以完整模型明确。

### RV-03：晚期网络、RT60、Modulation和空间输出

- **状态／优先级：确认限制／待测校准，P1。证据：E01._fdn_process实为四comb并联＋两allpass；Modulation为固定正弦LFO；湿输入折为mono。**
- **实施：** 可先保留校准好的Schroeder方案；更高密度目标采用8线正交反馈FDN与每线按延迟确定RT60的增益、分频段阻尼；ER随机低频调制如需对标采用带限、种子可重现控制过程。
- **步骤：** 先建立IR／EDC／echo density基准；比较两个成熟方案CPU／音质；选一个稳定实现，避免同时维护互不一致路径；真实多声道输出共享布局契约。
- **验收：** 0.5/1/2/4s目标RT60在所定义中低频误差≤10%；长尾无周期啸叫或能量增长；mod=0无调制，打开后变化连续；预览／导出种子和时间轴一致；多声道不是L/R交替复制。

### RV-04：网络热更新、输出三路与尾音

- **状态／优先级：确认缺陷风险／待测对齐，P1。证据：E01._rebuild重建ring/combs/allpasses；输出Direct/ER/Rev和dry_wet组合。**
- **实施：** 参数分类：gain/mix ramp、tone平滑、RT60就地反馈更新、几何读头迁移或双网络交叉淡化；三路输出与Dry/Wet采用可说明的同一混合公式。
- **步骤：** 保留网络容量和状态；区分用户Reset和参数拖动；依赖MX-07尾音；测静音／旁路／seek后网络行为。
- **验收：** 拖RT60／尺寸／distance时湿声不突然归零；Solo三路可解释；最小推子有明确静音语义；自然片段尾持续，seek旧尾消失；图只是模型估计时明确标识。

## 11. 公共预设、A/B、参数版本与失败路径

### ST-01：效果器用户预设持久化

- **状态／优先级：确认缺陷，P1。证据：E08/E10/E11._save_new_preset仅append窗口内列表；EQ/Panner同类入口也需一起复核。**
- **实施：** 复用现有用户预设存储，建立EffectPreset schema：类型、版本、参数、可选声道约束、名称和稳定id；原子写入；内置／用户预设分开管理。
- **步骤：** 保存／加载／重名／删除／导入导出；外部source引用符号化；损坏文件隔离并可恢复；不存机器路径和活QObject。
- **验收：** 关窗口、重启、新工程均可加载；损坏单条不丢整库；未知字段保留；另一个工程没有该SC源时提示，不误绑同名轨。

### ST-02：A/B和外部回填的一致性

- **状态／优先级：确认限制，P1。证据：E10/E11 bank仅窗口内；E08当前无bank；set_current_params只回填当前bank。**
- **实施：** 明确A/B是临时试听还是工程状态；若要持久化，模型保存两组和active；复制不改当前音频，切换走command；外部undo/redo回填不误覆盖另一bank。
- **步骤：** 定义bank模型／scope；增加切换、复制、关闭重开策略；与整体Mixer snapshot区别开，避免两套真源。
- **验收：** A→B→A完全恢复；复制到非活动bank不改变声音；外部undo后控件／bank／DSP一致；临时模式关闭会丢失时有清晰产品契约。

### ST-03：schema迁移、能力声明和故障处理

- **状态／优先级：确认限制，P0。证据：E12参数变化；Mix、EQ斜率、MakeUp位置、输入格式等修正会改变旧声音。**
- **实施：** 每效果保留algorithm_version/schema_version；迁移区分“保持旧声音”和“显式升级到新语义”；preview/export声明同源能力，后端缺失不静默旁路。
- **步骤：** 准备旧版本fixture；未知effect与未知参数保留；必要时提供兼容处理器；参数NaN/Inf和非法布局明确诊断；记录降级原因。
- **验收：** 旧工程打开、保存、重开不丢字段；用户升级后undo可回退；不支持组合导出前阻止或明确选择降级；取消／失败释放所有音频资源。

## 12. 分阶段实施与依赖

| 阶段 | 任务 | 前置 | 完成标志 |
|---|---|---|---|
| A 证据冻结 | 真实视频基线、数值缺陷fixture、字段覆盖表、处理顺序文档 | 无 | 可重复复现且不依赖人工目测 |
| B 状态与读数 | MX-01/02/06/12，DY-01状态，DL-01双提交 | A | 停止全清、实例身份稳定、无孤儿窗口、一次手势一次提交 |
| C 数学正确性 | EQ-01，PAN-01，DY-02/04，RV-01 | A、B基础DTO | 图／DSP同源，所有已复现数值bug有通过回归 |
| D 统一图与尾音 | MX-04/05/07/08、DL-03、RV-04 | B、C、版本策略草案 | 轨链一次处理、无输入drain、侧链预览导出同构 |
| E 规范状态恢复 | MX-11、ST-01/02/03 | B、D schema确定 | 全字段capture/apply/undo/reopen一致 |
| F Master／Bus与格式 | MX-03、MX-02扩展、MX-13/14/15、真实路由／LFE基础 | D、E | 普通轨／Bus／Master均有真实处理和遥测 |
| G 时间与分析 | MX-09/10/16/18、DY-03、DL-02、RV-02 | D、F | 采样率、自动化、平滑、FFT、表计和设备验证通过 |
| H 空间与混响成熟化 | PAN-02/03、RV-03 | F、G、多声道设备或离线验证 | VBAP／真实LFE／校准晚期网络和输出布局成立 |
| I 发布验证 | 端到端工程、交付、性能、生命周期、独立Review | A–H范围完成 | 无未处理P0；P1限制明确，不以UI完成替代能力 |

阶段B/C可以在明确修改边界后分工；D中的编图、命令和导出变更必须由同一接口约束协调，不能各自实现第二套规则。每阶段先Review方案、再最小实现、再Review与focused tests；不要求一次重写全部音频系统。

MX-17及BPM／True Peak／卷积混响等P2增强在阶段I之后单独立项；优先交付准确、稳定的剪辑混音核心。

## 13. 量化验证矩阵

所有阈值为本项目建议验收目标，不能当成已测得的Fairlight专有数值。高频接近Nyquist、极低电平、随机调制等场景单列容差。

| 层级 | 输入／组合 | 核心判据 | 关联任务 |
|---|---|---|---|
| 纯函数 | EQ SOS、动态transfer、Panner矩阵、反馈矩阵 | 独立解析oracle；有限值；对称性；稳定性 | EQ-01、DY-02、PAN-01、DL-02 |
| DSP块 | impulse、sine、sweep、pink/white noise、DC、静音、反相立体声 | 传输、IR、EDC、tail；整块／随机分块≤1e-5 | 全部效果 |
| 参数热更新 | gain/threshold、Delay121→300ms、RV亮度/尺寸/RT60 | 状态连续、无全湿骤清；无意外DC／爆音 | MX-07、DY-03、DL-02、RV-01/04 |
| 多实例 | 同类型A/B调序／删除／复制／旁路 | id/state/tap对应，删除不转移历史 | MX-06 |
| 同轨多clip | 重叠／间隔／淡化／retime | sum→track-DSP一次；顺序无关；尾音不断 | MX-04/07 |
| 侧链 | Track↔Track、Bus↔Track、Bus↔Bus、缺源／循环 | 检源时间对齐；raw listen；预览导出一致 | MX-08、DY-04 |
| Mixer | 普通轨／嵌套Bus／Master、pre/post send、顺序切换 | 图与模型一致；路由不静默掉音 | MX-03/05 |
| 窗口 | 同时开五窗、关窗重开、删轨、换工程、100次开关 | 无孤儿／订阅泄漏／迟到提交，FFT按需 | MX-12 |
| 播放状态 | play→pause→play→stop→seek，静音/solo/空clip | 两个UI周期内当前表归零，无旧代数回亮 | MX-01 |
| 工程 | 旧／新schema、save/reopen、preset、snapshot、undo/redo | 全字段等价，未知字段和引用处理正确 | MX-11、ST-01/03 |
| 实时 | 44.1/48/96kHz；24/29.97/59.94fps；1/8/16轨 | block p95/p99、underrun、CPU记录；不假设平均值代表实时 | MX-04/07/10、DY-03 |
| 输出 | 实时PCM截取对比离线PCM；stereo／多声道／stem | 对齐后≤约定误差；路由、延迟、tail、SC一致 | MX-03/07/08 |
| 失败 | NaN/Inf、坏预设、后端缺失、设备切换、取消导出 | 明确诊断；不丢工程字段；线程／资源清理 | ST-03 |

测试工程至少包含：真实视频A1、第二条检源轨、嵌套Bus、Master、两同类Delay、重叠clip、片段后空隙和序列末尾。真实视频证明集成；合成信号负责证明数学行为，二者不能互相替代。

性能预算：

- 48kHz／256样本时一个块约5.33ms；总DSP必须留解码／输出／调度余量，建议目标DSP p99不超过块时长的50%，具体硬件基线另记。
- 当前后端实际使用的较大块也须测量，不能把测试块改大来隐藏交互延迟；记录端到端AudioSink延迟。
- UI约30Hz合并更新；UI关窗不应改变音频结果，FFT关门应减少开销。
- 如果逐样本hold、fractional-delay、FDN需要编译实现，采用项目可维护的原生／向量化方案，提前验证安装包、取消、线程和采样率变更。
- 多声道能力可以先离线逐通道验证；没有对应硬件时报告“离线路由已测、实机监听未测”，不得宣称全链路验收。

## 14. 算法参考骨架

以下示例展示成熟方案的接口与数学，省略调度、输入校验、缓存、迁移和资源处理。实时生产实现应预分配、避免UI回调／动态分配／Python逐样本热点，并由统一音频graph调用。

### 14.1 Butterworth斜率与共享响应

~~~python
import numpy as np
from scipy.signal import butter, sosfilt, sosfreqz

def cut_sos(kind: str, cutoff_hz: float, slope_db_oct: int,
            sample_rate: float) -> np.ndarray:
    if slope_db_oct not in (6, 12, 18, 24):
        raise ValueError("unsupported slope")
    order = slope_db_oct // 6       # 6/12/18/24 -> 1/2/3/4阶
    if kind not in ("highpass", "lowpass"):
        raise ValueError("unsupported cut kind")
    if not 0.0 < cutoff_hz < sample_rate * 0.5:
        raise ValueError("cutoff outside Nyquist")
    return butter(order, cutoff_hz, btype=kind,
                  fs=sample_rate, output="sos")

def response_db(sos: np.ndarray, hz: np.ndarray,
                sample_rate: float) -> np.ndarray:
    _, h = sosfreqz(sos, worN=hz, fs=sample_rate)
    return 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))
~~~

DSP用同一个SOS和跨块zi运行sosfilt，UI直接画response_db；Bell/Notch/Shelf系数按RBJ及明确Q/S契约生成。不是把一个二阶段重复N次来模拟N阶。奇数阶的SOS仍可容纳一阶段，不丢掉6/18dB选项。

### 14.2 Compressor／Expander静态传输

~~~python
import numpy as np

def compressor_gain_db(level_db: np.ndarray, threshold_db: float,
                       ratio: float, knee_db: float) -> np.ndarray:
    x = level_db - threshold_db
    slope = 1.0 / ratio - 1.0
    if knee_db <= 0.0:
        return slope * np.maximum(x, 0.0)
    lower, upper = -0.5 * knee_db, 0.5 * knee_db
    return np.where(
        x <= lower, 0.0,
        np.where(x >= upper, slope * x,
                 slope * (x + 0.5 * knee_db) ** 2 / (2.0 * knee_db))
    )

def expander_gain_db(level_db: np.ndarray, threshold_db: float,
                     ratio: float, range_db: float) -> np.ndarray:
    under = np.maximum(threshold_db - level_db, 0.0)
    return -np.minimum(range_db, under * (ratio - 1.0))
~~~

检测器输出与原始audio分离，原始audio用于Listen。Gain computer在明确的检测域运行；时间响应使用采样率换算，gain平滑与detector平滑的选择必须固定，不能让UI图和声音分别选择不同模型。

### 14.3 Hold和因果延迟／尾音契约

~~~python
# 状态机伪码；生产实现放编译／高效DSP内核。
# 以Gate为例：open_threshold > close_threshold形成迟滞。
if detector_db >= open_threshold:
    hold_left = hold_samples
    gate_open = True
elif detector_db < close_threshold:
    if hold_left > 0:
        hold_left -= 1
    else:
        gate_open = False
target_gain = 1.0 if gate_open else db_to_gain(-range_db)
gain = smooth_towards(target_gain, gain, attack_coeff, release_coeff)
~~~

时间常数可用exp(-1/(tau_seconds*sample_rate))，但它是RC响应定义，不意味着tau时已经完全达到目标。增加lookahead时节点必须报告latency_samples，所有汇合支路补齐；不能只把目标轨推迟而侧链／干声／视频不同步。

~~~python
# 图调度伪码：即使没有clip，活动尾音也须推进。
if track_input_exists:
    input_block = summed_clip_block
elif node.tail_active:
    input_block = preallocated_zero_block
else:
    skip_node()
output_block = node.process(input_block)
node.update_tail_state(output_block)
~~~

tail_active不能仅凭单个输出块为零立即关掉：Delay在第一次回声到达前可能全零。要结合网络状态、最大尚未读出的延迟和能量门限。

### 14.4 Panner安全矩阵与VBAP

~~~python
import numpy as np

def apply_stereo_matrix(samples: np.ndarray,
                        matrix: np.ndarray) -> np.ndarray:
    out = samples.copy()
    out[:2] = matrix @ samples[:2]  # 两行都读取原samples
    return out

def vbap_basis_gains(direction: np.ndarray,
                     speaker_basis: np.ndarray) -> np.ndarray:
    # basis列为一组扬声器单位向量；2D为2x2，3D为3x3。
    gains = np.linalg.solve(speaker_basis, direction)
    if np.any(gains < -1e-9):
        raise ValueError("direction outside this basis")
    gains = np.maximum(gains, 0.0)
    return gains / max(np.linalg.norm(gains), 1e-12)
~~~

预计算有效基底与逆矩阵；运行时选择含目标方向且gain非负的一组。Spread可对多个邻近方向求能量合成后归一；Divergence按邻接布局分配。LFE单独路由，不参与全频VBAP归一，也不是低频复制回L/R。

### 14.5 Fractional delay、平滑与稳定反馈

~~~python
import numpy as np

def fractional_read(ring: np.ndarray, write_index: int,
                    delay_samples: float) -> float:
    pos = (write_index - delay_samples) % len(ring)
    i0 = int(np.floor(pos))
    t = pos - i0
    return (1.0 - t) * ring[i0] + t * ring[(i0 + 1) % len(ring)]

def feedback_matrix(left: float, right: float,
                    blend: float) -> np.ndarray:
    f = np.array([
        [(1.0-blend)*left, blend*right],
        [blend*left, (1.0-blend)*right],
    ])
    if max(abs(np.linalg.eigvals(f))) >= 1.0:
        raise ValueError("unstable feedback")
    return f
~~~

示例线性插值可先验证语义，高质量目标评估Lagrange等插值的幅频和负载。时间拖动可在旧／新读头间短交叉淡化；若选择连续变速读头，明确会有Doppler音高变化。不可每次拖动都清空ring。

### 14.6 Image-source ER与8线FDN

~~~python
import numpy as np

def first_order_images(source: np.ndarray,
                       room: np.ndarray) -> list[np.ndarray]:
    images = []
    for axis in range(3):
        near = source.copy()
        far = source.copy()
        near[axis] = -source[axis]
        far[axis] = 2.0 * room[axis] - source[axis]
        images.extend((near, far))
    return images

def er_delays(source: np.ndarray, listener: np.ndarray,
              room: np.ndarray, pre_ms: float,
              sample_rate: float) -> list[float]:
    taps = []
    for image in first_order_images(source, room):
        path_m = float(np.linalg.norm(image - listener))
        delay_s = max(0.0, path_m / 343.0 + pre_ms / 1000.0)
        taps.append(delay_s * sample_rate)
    return taps

def fdn_feedback(delay_samples: np.ndarray, rt60_s: float,
                 sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    n = len(delay_samples)
    # Householder正交矩阵；n=8可作为成熟FDN起点。
    q = np.eye(n) - (2.0 / n) * np.ones((n, n))
    gains = 10.0 ** (-3.0 * delay_samples / (rt60_s * sample_rate))
    return q, gains
~~~

FDN每个时刻读出各延迟线，分频段阻尼后乘每线g，再用正交矩阵混合并加入输入。不同频段目标RT60决定反馈滤波；最大Brightness可设计为无额外频率衰减。输出矩阵、注入向量、互质延迟和扩散需经echo-density／EDC／听音校准。上述只是模型骨架，不是完整房间模拟，也不保证与Resolve相同IR。

### 14.7 环窗与不会相消的立体声频谱

~~~python
import numpy as np

def stereo_power_spectrum(window_lr: np.ndarray,
                          write_index: int) -> np.ndarray:
    ordered = np.roll(window_lr, -write_index, axis=-1)
    win = np.hanning(ordered.shape[-1])
    spectra = np.fft.rfft(ordered * win, axis=-1)
    # 能量合成而非先(L+R)/2，因此L=-R仍保留能量。
    power = np.mean(np.abs(spectra) ** 2, axis=0)
    return power
~~~

再按明确的窗coherent gain或能量归一转换dB；peak幅度、RMS功率与LUFS不能共用未说明的归一。Mid/Side可以额外显示，但必须与stereo power命名区分。

## 15. 发布证据与最终完成标准

每个阶段交付：

1. 问题、最终行为、模型／schema／能力变化和兼容决策。
2. 能暴露原错误的focused test，不只断言新实现细节。
3. 真实音频／IR／GR／EDC／路由／CPU测量结果及版本、采样率、块大小。
4. 独立Review问题和修复记录；线程、资源、取消、旧工程和导出路径均审查。
5. 当前UI可用范围、已知限制、明确待办；不留下“按钮能按所以功能已完整”的结论。

完成门槛：

- 所有已确认P0完成；P1限制由产品明确接受或继续实施。
- 五款效果器每个可见有效参数都能通过声音／数值／路由证明；无效能力明确禁用或不暴露。
- 普通轨／Bus／Master、播放／导出、参数／图形／遥测／持久化各有一致真源。
- 停播清表、无输入尾音、实例调序、旁路和窗口删除通过生命周期回归。
- 用户视频真实主窗播放和独立合成测试均通过，最终产物附证据。
- “成熟公开方案已实现”与“完全复刻Fairlight专有音色”明确区分，后者不作为未经测量的承诺。
