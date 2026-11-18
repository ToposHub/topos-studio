# Topos Studio 影视声音后期复核与下一阶段计划

## 复核结论

复核基线为 `fix-ui` 的 `7ec98f4c`。复跑上一份计划的 968 项定向测试，全数通过。新加的检查覆盖真实控件入口、不同源入点的预渲染、处理栈数据保留和试听设备短写。原计划已形成可用的立体声工作区骨架，交付一致性仍有可复现的阻塞项。建议先打通一个真实片段从剪辑、混音到 Master 和 Stem 的全流程，再扩大效果器和环绕格式。 [原计划](2026-11-18-clip-editor-and-mixer-roadmap.md) · [原完成报告](2026-11-18-clip-editor-and-mixer-roadmap-status.md)

| 原计划模块 | 本次判定 | 核对结果 |
| --- | --- | --- |
| Media Pool、Effects、Audio Timeline | 已落地 | Edit 与 Mixer 共用面板和工程，已有只导入音频的拖放和音频插槽。下一步统一 Effects 双击、拖放、插槽的目标与能力判断。 |
| Clip Editor 试听、波形、频谱 | 可操作，有正确性及性能缺口 | 已有真实 PCM 和独立输出会话。重开已处理片段时，异步 PCM 就绪只回填原音。频谱、响度及部分 DSP 仍在 UI 回调里整段运算。 |
| 处理栈、降噪、频谱修复 | 已有算法和持久化，交付待对齐 | 单步效果与本地验证成立。离线与实时步骤混排、改时长超出源结尾、不同输出采样率及多声道映射有以下阻塞。 |
| 包络、标记、反极性、时间编辑 | 部分完成 | 片段包络可写入模型并进入图，时间线只有叠加显示。声道菜单入口、试听静音/独奏、基于处理后结果的计量，以及改时长反向事务仍待建立。 |
| 轨道自动化、Send、录音与交付 | 有基础能力 | 有按轨 Read/Touch/Latch/Write/Off、录音设置、BWF 和 Stem 入口。需完成图级预卷、监听隔离、硬件录放和实际交付文件 QC。 |

本次直接修复了以下局部问题。

- 真实 Stop、选区处理、频谱修复和时间/音高预览按钮按 PCM 可用状态启用。切换片段或重连时，先停掉并释放旧试听样本。试听处理设备短写，循环时等待设备播放完送入的样本，停止回到本次起听点。反极性立体声的噪声底按各声道功率统计。
- 预渲染缓存键加入源入点和方向，旧缓存按新版本重建。移除最后一项或清空处理器保留标记、包络及其他持久字段。暂时阻止会让片段长度失配的栈旁路、删除和清空，提示用时间线撤销。链接音画的长度确认对话框现在实际显示独立编辑复选框。
- 混音通道添加菜单补回已有生产后端的 Voice Changer 和 Spectral Mangler。Effects 双击 Spatial Panner 写入音轨的声像字段，错误目标被拦截。Send 徽标排除关闭、静音和零电平的发送。

## 第一阶段 · 交付正确性

| 优先级 | 工作 | 实施方法 | 验收 |
| --- | --- | --- | --- |
| P0 | 统一 48 kHz 编辑坐标与消费采样率 | `audio_edit` 区间以 48 kHz 整数持久化。用 `TimelineSampleClock` 和有理数转换建立贯穿起止点的映射。第一版可统一在 48 kHz 混音后做一次交付重采样。若图运行在交付采样率，必须重采样基底并用绝对位置求区间、边缘和包络。 | 实测现版 `mute[48000,96000)` 在 48 kHz 约落于 1–2 秒，在 96 kHz 落到约 0.5–1 秒。用 44.1／48／96 kHz 复测脉冲、斜坡、fade、零点和忙闲块，边界偏差不超过一个输出样本。 |
| P0 | 将预渲染后的 PCM 当作独立源 | 给实时、成片、Stem 共用同一个 clip block provider。先查渲染状态，再读取当前 revision 的成品 PCM 和其大小。源解码负责输入，缓存成品负责有效输出范围。历史缓存可在编辑试听中保留为显式旧版，交付锁定同一 revision。 | 实测真实 1 秒 WAV 以 50% 播放生成了 2 秒缓存，实时第二秒仍全静音。补成片和 Stem 的同例，最后一帧有应有能量。验证外录绑定、非零入点、延长后播放头、渲染取消和原源 EOF。 |
| P0 | 按用户机架顺序执行并为交付保真 | 拿到最后一个需要烘焙的步，把它及其前面的所有已启用步骤作为缓存前缀，按顺序处理。剩余的后缀才走实时块。版本包含全部前缀，变更任何有效字段生成新 key。`pending/ready/failed/cancelled` 用于预检、导出 worker 和 UI，导出读不到可信 PCM 时中止并报告片段、步骤和原因。 | 比较 `Gain→Denoise` 与 `Denoise→Gain` 的非通勤 fixture，再验证重排、旁路、同一区间反复交付、缓存损坏和后端缺失。故意令真实构建失败，确保成片或 Stem 没有以成功结果静默丢效果。 |
| P0 | 明确声道与源 patch 的先后顺序 | 解码保留源布局和通道名，在与 Clip Editor 一致的通道域处理栈，再到 clip patch、轨道布局与路由图。给 pre-render spec 加 codec stream ID、layout、源指纹和处理前后域。普通跨布局映射走现有矩阵，保留单独 LFE 策略。 | 双单声道带独立脉冲的 BWF、Poly WAV 的逐声道选择、5.1 与 7.1 分别做 one-hot 脉冲。导出声道 order、音量、零串漏和 LFE 正确，2.0 upmix 与原生环绕明确标识。 |
| P0 | 重开与切换预览使用同一已处理数据 | `ProjectClipEditor._apply_pcm` 不再以源 PCM 当栈处理结果。独立 worker 按 `(spec, steps, view, fft, generation)` 发布当前/原始预览与波形、频谱、计量，沿用 PCM/预渲染缓存。旧回调必须释放。引入可取消、同 key 合并、失败清理和可结束 worker。重开已有静音栈应直接试听正确版本。UI 只做差分投影。 | 复现用 48 kHz、1 秒、振幅 0.25 的立体声 WAV 保存全段静音栈。重开时原代码首次试听仍为 0.25，切一次 A/B 才变为 0。修复后冷启加载静音和降噪，比较第一次播放、A/B、Edit 实播和成片。压测来回切 20 个片段和 300 秒素材，首波形延迟、内存峰值、GPU 帧间隔与设备欠载都有记录。故意令 decode 返回空、重连或退出，旧回调不误写。 |
| P0 | Master、Stem、预卷与延迟补偿 | 以共享 block scheduler 推进 Clip、Track、Bus、Master 的同一绝对时间，复用当前音频图，统一 sidechain、Send tap、自动化、PDC（插件延迟补偿）和 `latency/tail`。对范围导出先预卷状态，再从交付起点写文件。明确可计入的效果尾长及 Stem 的 pre/post Master 语义。 | 不同块长下同一毫秒的脉冲落点一致。用台词侧链压音乐、两段串联 delay、Bus 反馈环拒绝和跨区间长尾测试。线性图且同一取点时分类 stems 数学求和与合成对得上。Master 有非线性处理时另测预期差异。 |

优先在已有纯函数和生产入口上实施。当前数据边界见 [预渲染缓存](../../src/features/edit/core/pcm_render_cache.py) · [片段处理函数](../../src/features/edit/core/audio_edit.py) · [播放](../../src/features/edit/core/audio_playback.py) · [成片](../../src/features/edit/core/timeline_export.py) · [Stem](../../src/features/edit/core/stem_export.py) · [Clip Editor](../../src/features/mixer/ui/project_clip_editor.py)。混音台的版本迁移、PDC、尾音、Bus/Master 自动化及创意效果器历史回归见 [上一次音频效果复审](2026-11-15-audio-fx-post-integration-review.md)。其中的历史案例应在当前 HEAD 重跑。

## 第二阶段 · 影视对白编辑与混音

| 优先级 | 功能 | 实施方法 | 验收 |
| --- | --- | --- | --- |
| P1 | 时间线包络编辑 | 保留现有模型折线和 Clip Editor 入口。在 `ClipItem` 新增节点命中、Option/Alt 添加、节点/曲线拖动与选择，采用局部采样时间和 dB。拖动只更新临时 overlay，松手提交一次 `UpdateClipAudioEditCommand`。用独立编辑模式处理 Delete、Esc 和吸附。 | 两页编辑同一点，零点及 `0→−12 dB` 中点准确。一条节点操作只占一步撤销。覆盖 slip、split、倒放、变速、锁轨、冻结及超过 1000 节点的帧时间。 |
| P1 | 选区增益、监听、计量和标记 | 现有整段增益保持可用。L/R、Mute、Solo 接真实监听设备 buffer。反极性仍走工程步骤。A/B 仅切渲染 tap。仅放行已接后端的右键菜单项。新增区间/单声道 `range_gain`、时间线/片段标记稳 ID 和命令。Normalize/LUFS 对实际插入阶段、声道与同一选区运算，静音无解时提示，允许实际衰减或提升，写入后复测目标和 dBTP，重复同约束不叠同一个增益。 | 互异 L/R 音调分别试听或反极性，导出证明监听状态无副作用。给 `−10 LUFS` 目标 `−23 LUFS` 应衰减。测静音、上限、已有 gain/denoise、A/B 和连击结果。点击菜单/按钮有覆盖，且按原选区起点输出。 |
| P1 | 改时长步骤的反向编辑 | 统一启用、禁用、替换、删除和清空的长度变更事务，重算本轨、链接 A/V、Sync Lock、后续自动化、转场及序列标记。状态依有序映射从源坐标重新推导，保留对删段导致不可映射的提示。原生 retime/倒放和片段栈复用同一输出时间映射。 | 先删区段，再局部变速，旁路前一项，确认边界、包络、标记、淡化和 A/V 语义。gap/ripple、锁轨冲突、撤销与再次保存复原。超出兼容范围时状态清晰可撤销。 |
| P1 | 对白剪辑工具 | 基于现有 ADR/录音、媒体重连和选区工具扩展 Room Tone 填补、J/L cut、批量短交叉淡化、切空不动同步、对白/ADR 匹配、take 轮换与 audition lanes。加场次镜次、麦克风 source patch、同场对话编辑组。以离线相关估计建议对齐，确认后写子帧偏移。 | 模拟 3 个镜次、领夹/吊杆双源、环境底噪补段与 5 条 ADR cue。源保持可追溯，切点无爆音，选中 take、淡化和波纹状态重开一致。 |
| P1 | 自动化与 VCA | Track/Bus/Master 统一 Read/Touch/Latch/Write/Off、预听与提交，增加时间线参数 lane、Trim Automation、写保护与自动化预览捕获。VCA 作为增益/静音控制层，管理成员和快照，保持路由和 Send tap 不变。 | 分轨与组同时运动，断触、切页、关工程和单次 Undo 的样本曲线正确，Bus/Master 入导出。VCA 与手动电平和 pre/post send 的倍率按模型可测。 |
| P1 | 监控与录音 | 分开 Arm、Input Monitor 与 Record。优先接统一监听图、设备切换校准、录音素材和 cue 管理。支持独立监听音量、Mono、DIM、Mute、Solo-safe/PFL/AFL、参考校准。用设备实际 block 往返测延时，BWF 写 sample reference。 | macOS 和 Windows 至少各一套真实设备，测试 48/96 kHz、双输入、预卷、录音期间切页、掉线和异步关闭。台词回放位置对 picture 偏差满足验收阈值。设备监听控制不会改 Master/Stem 文件。 |
| P1 | 交接与自动 QC | 建立 AAF 互换测试子集（轨道、源引用、裁剪、handles、淡化、音量、时间码），先支持常用交接，再考虑未支持轨与复杂效果的 bake。BWF 交付到 Full Mix、DX、ADR、Foley、FX、MX、M&E，保存起始 TC、帧率/DF、声道顺序、峰值和校验摘要。预设按客户规范分版本，包含标准、目标、容差及报告。 | 导入来自两套独立后期工具的短 AAF，再导出检查手柄、链接媒体和偏移。交付每条 stem 共用起止样本。对照剥离效果并核验缺素材、削波、过量静音、响度、mono compatibility 和包校验。 |
| P2 | 画面改版 Reconform | 利用镜头 ID、源时间码、内容指纹和版本对照，输出插删、替换、移位的候选变更表。编辑者逐段确认或回滚，处理音乐连贯、音频 handles 和保护轨。 | 对 30 分钟节目做删场、换镜、重排或 23.976 到 25 fps 的三类改版，对齐误差有证据，ADR/FX/音乐不丢。 |
| P2 | 原生 5.1／7.1、对象音频 | 从明确的 bus layout、输出 patch 和设备监看开始，复用矩阵与 VBAP。做多声道 FX 的能力查询与降级。再评估 ADM 的对象、轨道 UID 和 bed 导出。 | 多通道 null test、路由标签、downmix、LFE 管理、环绕电平和外部播放器核验。只有已完成真实输出、导出与回读的格式才开放交付入口。 |

可直接复用 [录音管理器](../../src/features/edit/core/recording_manager.py) · [BWF 读写](../../src/features/edit/core/bwf.py) · [互换能力状态](../../src/features/edit/core/interchange.py) · [轨道自动化](../../src/features/edit/core/automation_recorder.py) · [声道能力](../../src/features/edit/core/layout_capability.py)。交接与测量按公开规范核验。 [AAF 开发资料](https://aaf.sourceforge.net/) · [EBU Tech 3285](https://tech.ebu.ch/docs/tech/tech3285.pdf) · [EBU R 128](https://tech.ebu.ch/publications/r128/) · [ITU-R BS.2076](https://www.itu.int/rec/R-REC-BS.2076)

## 第三阶段 · 从 Topos test 接入值得做的效果器

核对了 Topos test 顶层 24 个 Python 文件，源文件 SHA-256 均与导入清单匹配。它们对应 23 款效果器和 Clip Editor。仓库已收录全部 UI，生产面板通过共享轨道条和效果注册表加载已接通的 DSP。原型的参数快照可作为设计输入，接通时需逐个映射到实际生效的 production schema。 [原型来源清单](../../src/features/mixer/effects/source_manifest.json) · [原型参数表](../../src/features/mixer/effects/parameter_schemas.json) · [生产效果注册表](../../src/features/edit/core/effect_registry.py) · [DSP 链](../../src/features/edit/core/audio_dsp.py)

### 已有生产对应能力

| Topos test 原型 | 生产入口 | 下一步 |
| --- | --- | --- |
| `EQ-UI.py` | `strip_eq` | 复用生产 EQ 面板、曲线求值和版本迁移。检查六段 UI、实时频谱 tap 与已有 presets。 |
| `Dyamics-UI.py` | `dynamics`。通道条用 Expander、Compressor、Limiter 组成 Dynamics 组 | 复用算法与现有侧链。迁移时保留旧组和复合实例各自的 Make Up 位置。对应控件有 Clip/Track/Bus/Master 公用 schema 后再合并。 |
| `Delay-UI.py`、`Reverb-UI.py` | `delay`、`reverb` | 补延迟和尾音 PDC、sidechain/send 复测与多通道能力表。保留可交付的现有算法。 |
| `Saturation_UI.py`、`Stereo_Imager(M:S)_UI.py` | `saturation`、`imager` | 当前可用核心 Drive、Pan/Width。把分频、多引擎、过采样、M/S、Mono Maker 和实测相关度分别评估、测试，再暴露。 |
| `Panner-UI.py` | `spatial_panner`，轨级属性 | 立体声路径优先。等多声道图通了再打开真实的前后、高度和环绕输出参数。 |
| `VoiceChanger-UI.py`、`SpectralMangler-UI.py` | `voice_changer`、`spectral_mangler` | 已有生产 DSP、Clip FX 和通道条入口。先验 Neutral、重开、随机块长、seek、延迟、尾音、多实例和实时性能，再扩 Vocoder、Freeze 或高阶颗粒能力。 |
| `AudioClipEditor-UI.py` | 独立 Clip Editor | 继续做修复、编辑及试听入口，Clip FX 插槽仍用于可参数化的生产 DSP。 |

### 推荐接入顺序和 DSP 验收

| 顺序 | 原型及适用位置 | 实施与验收 |
| --- | --- | --- |
| 1 | **De-Esser** `DeEsser_UI.py`，对白片段、ADR、对白 Bus | 检测频带使用可调 band-pass，对 RMS 或整流包络做 attack/release 和软拐点，限制最大 GR。先做 Wide，再做用匹配滤波分频的 Split，支持隔离 detector listen。已有压缩器的检源和遥测可复用。lookahead 在有 PDC 后开放。用对白和 1 kHz+6 kHz 双音验收，齿音组能衰减 6 dB、Split 下低频 ≤0.5 dB 变化，普通对白保持且旁路无跳变。 |
| 2 | **De-Click / De-Hum** `DeClick_DeHum_UI.py`，优先 Clip Editor。持续去嗡声也适合 Track FX | De-Hum 对 50/60 Hz 与所选谐波做窄 notch，自动模式用安静窗 PSD、邻域显著性和候选频漂。De-Click 以高通残差和滑窗 MAD 检测短离群，跨声道置信度及宽带瞬态 veto 保护辅音。候选点用短区间双侧插值，长缺失转频谱修复。利用现有预渲染栈、波形和 residue 试听。验收 50/60 谐波抑制 ≥15 dB、有用频带 ≤0.5 dB，定位稀疏点击，鼓点及爆破音保真。 |
| 3 | **True Peak Limiter** `true_peak_limiter_ui.py`，Master 和交付 | 独立版本化效果器，现有 `LimiterEffect` 和 `MasterLimiter` 保持旧工程语义。用有状态多相 FIR 过采样、lookahead、链接/独立通道增益与平滑 release，声明 group delay 并接 PDC。用现有响度测量的 dBTP 路径做独立复核。Dither 是最终 PCM 量化选项，不能写入内部浮点链。验收高频相位、采样间超限、随机块长、多通道链接、尾音、旁路音量与交付重采样后的上限。 [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I) |
| 4 | **Dynamic EQ** `dynamic_eq_ui.py`，对白/音乐避让、轨道和 Bus | 复用 `strip_eq` 系数与形状，接每段 detector、attack/release、最大提升/衰减和受信插件身份。第一版最小相位、内检源。外检源在 sidechain 稳定后，线性相位在 PDC 齐备后。验收关闭动态为同版本 EQ，目标频段压制、块边缘无跳变、旁路一致，自动化区间可重开。 |
| 5 | **Bus Compressor** `bus_comp_ui.py` 与 **Multiband Compressor** `multiband_comp_ui.py` | Bus 版本先绑定现有 Compressor 核心和 DX/MX 胶合模板，支持并行 Mix、SC 高通。等待单独测试后才开放多种电路拓扑和 Auto Release。多段先实现相位对齐的互补分频与每段独立 envelope。线性相位分频另定延迟合同。验证等增益时合成响应、外检源、全旁路、跨带、长尾和 sidechain 报告。 |
| 6 | **Convolution Reverb** `convolution_reverb_ui.py`，Room Match、ADR、环境和 FX Bus | 将 IR 以资源 URI 登记并校验指纹，统一重采样、裁切和规范化。用分区 FFT，端点加窗，声道矩阵支持 mono→stereo 和有规格的 true stereo 四路径，精确报告延迟和尾长。单位脉冲复现 IR 样本，交叉路径与轨道标签正确，坏文件可重连，移动工程后导出一致。 |
| 7 | `transient_designer_ui.py.py`、`Chorus:Flanger:Phaser_UI.py`、`TremoloAutoPan-UI.py` | 瞬态塑形可优先用于 Foley 和动作音效，之后做调制、颤音/Auto Pan。分别完善导出尾音、可自动化参数、相位平滑、tempo/free-run 跨 seek 与撤销后的连续性。 |
| 8 | `exciter_ui.py`、`subharmonic_ui.py`、`doubler_harmonizer_ui.py`、`Pitch_Correction_UI.py` | 剧情性设计需求明确时再做。Exciter/Subharmonic 用分频、带限谐波和 headroom 测试。Doubler/Pitch Correction 用 F0、延迟、通道相关性与读写自动化验证。先区分 Clip Editor 的改音高和音高校正的音符/调性追踪。 |

可用下面的 De-Esser 增益控制作为离线测试 oracle。频带包络由带通和独立的时间平滑生成，`over_db` 是检测值相对阈值，混音图中的滤波重构与过渡另行测量。

```python
over_db = max(0.0, detector_dbfs - threshold_dbfs)
reduction_db = min(max_reduction_db, over_db * (1.0 - 1.0 / ratio))
gain = 10.0 ** (-reduction_db / 20.0)
# Wide: gain * full_band
# Split: unaffected_band + gain * sibilance_band
```

每个接入批次用统一注册表登记稳定 ID、版本、目标作用域、参数值域、状态迁移、实时和导出能力。Effects、Clip Properties、Track/Bus/Master 插槽复用同一身份和工厂。原型中暂未接通的控件显式标注。用常用会话的 DX、ADR、FX、MX 四条链做旧工程加载、插入、实时拖动、撤销、按快照重开及成片验证，记录现场听感和实际 DSP 结果。

## 完成判据与试片

1. 先做 **P0 立体声试片**。用 10 分钟双系统对白、ADR、Foley、音乐和模拟客户交付清单，经历剪辑导入、节目画面、片段修复、录音、侧链、自动化、打印 full mix 和 stems、保存重开。保留可发布的合成 fixture、真实设备报告及旧工程声音对照。未知效果或渲染错误不能产出成功交付。
2. 再做 **对白工具与交接**。上线 De-Esser 与 De-Click / De-Hum，完成样本级时间线包络、重开/预览、QC、AAF 子集、48 kHz 真机录音和校准。交付预设显示标准来源和版本。广播、影院、流媒体分别用对应节目要求，不用一个固定 LUFS 目标覆盖所有场景。
3. 最后评估 **混音规模与格式**。扩 VCA、自动化写保护、卷积和多通道格式。以 90 分钟多轨工程记录工作集内存、音频设备 underrun、拖动 p95、DSP 块 p99 和导出时间。阈值先在目标机器和设备缓冲配置实测确定。ADM 和第三方插件宿主在多声道、PDC、状态隔离、故障恢复和互换验证通过后独立立项。

回归沿用 [原 968 项基线](2026-11-18-clip-editor-and-mixer-roadmap-status.md#回归基线)，并增加生产效果器和能力测试，以及本轮的真实按钮、缓存、音频设备短写、反极性噪声底、链接音画长度和效果作用域检查。最终离屏定向回归 1103 项全部通过。原报告中的 5 个无关的 Edit 全量失败不在本轮定向验收范围。真机监听、多声道路由和第三方 AAF 往返须用现场交付补测。
