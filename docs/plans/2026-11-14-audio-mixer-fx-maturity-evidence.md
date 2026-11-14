# 音频 Mixer 与五款效果器成熟化 — 实施证据与交付边界

- 日期：2026-11-14（首轮，阶段 A–E/G 部分）；2026-11-15（续批，阶段 F/G/H 全量）。
- 对应计划：[2026-11-14-audio-mixer-fx-maturity-plan.md](./2026-11-14-audio-mixer-fx-maturity-plan.md)。
- 代码基线：df749759 起（提交清单见 §1）。
- 性质声明：本文逐项记录"已实现（含测试）/ 核心已实现（边界注明）/ 未实施（依赖说明）"，
  不以 UI 或文档完成替代能力验收；不宣称 Fairlight 专有音色复刻。

## 1. 提交清单

| 提交 | 内容 |
|---|---|
| 713d52f2 | 阶段A：§1.3 全部数值缺陷的证据冻结 fixture（18 项 focused 回归） |
| a3d0c70b | 阶段C：EQ-01/PAN-01/DY-02/DY-04/RV-01/MX-06 DSP 修复 + DY-01/DL-01 契约 |
| 1fcac68c | 阶段D：MX-04 求和后单次轨链 + MX-07 尾音 drain + MX-08 侧链同构 |
| 3bb9da3c | MX-01：版本化静音 payload + 播放代数守卫 |
| 615b81e1 | MX-02：按实例 tap + MX-12 对话框生命周期 |
| 302b88b6 | MX-11：全字段预设/快照 + ST-03 算法版本 |
| 5555bc63 | EQ-01 段数契约测试更新（真 Butterworth 段数） |
| d241fc86 | MX-09 频谱正确性 + MX-13 推子硬零 + DL-02 分数延迟 |
| d6c6839c | 生命周期绑定修复（destroyed 闭包 NameError 根治） |
| 545ef546 | RV-04：几何热更新（网络缓冲保留） |
| de406f6f | 阶段I：证据文档 + MX-05 处理顺序契约（TRACK_CHAIN_ORDER/ORDER_VERSION） |
| 67870f81 | 独立审查修复：3 Critical（reverb 预设回填/lufs 类型/旧预设清路由）+ 3 建议 |
| c2ebee02 | DY-03 hold 边界 + EQ-02 Solo 同源锁 |
| d29bc44c | ST-01：Dynamics/Delay/Reverb 用户预设原子持久化 |
| d8f734b2 | ST-01 补齐：EQ/Panner 预设持久化 + 计划表状态修正（2026-11-15 批） |
| f35c089c | MX-03：Master/Bus 处理与遥测闭环（含实时 Bus 侧链缺口修复） |
| 6daa6e75 | MX-14：pre/post-fader Send + 独立 mute（模型 v2/命令/UI/引擎） |
| f4f14216 | MX-15：布局能力矩阵 + 逐声道脉冲全扫 + 高度布局显式拒绝 |
| b27e7d28 | DY-03 校准锁定 + RV-02 image-source ER/负 Pre Delay/Distance 只动 ER |
| 76ad5657 | MX-16 表计校准 + EQ-02 增益-带宽耦合（ALGORITHM_VERSION=3）+ EQ-03 锁 |
| 51ab3963 | MX-10：fx/{instance_id}/{param} 参数自动化（实时/导出/UI） |
| 1016a3cd | RV-03 8 线正交 FDN + PAN-02/03 VBAP/LFE 离线域（含 3 项基线失败修复） |

## 2. 逐项交付状态（对齐计划 §4.1 总表）

### 已实现（focused 回归锁定）

| ID | 交付 | 证据 |
|---|---|---|
| EQ-01 | HPF/LPF 真 N 阶 Butterworth（6/12/18/24 dB/oct 标称=实际，截止 -3.01dB）；Shelf Q 实际生效（Q 缩放 RBJ 变体，Q=0.7071 等价旧 S=1）；`strip_eq_band_sections/response_db/total_response_db` 共享服务——DSP 与 EQ 曲线 UI 同一来源；段系数 LRU 缓存 | tests/edit/test_audio_dsp_maturity.py::TestEQHPFSlope / TestEQShelfQ |
| PAN-01 | SpatialPanner 两路输出由未污染原输入计算（矩阵 oracle ≤1e-6）；镜像对称锁定 | TestPannerMatrixAlias |
| DY-02 | Expander 标准 (ratio-1) 下扩斜率（1:2=20dB，Range 封顶）；Compressor hard-knee oracle（-9dB）；检测域统一为线性峰值跟随；Gate 独立迟滞状态机（开门/关门/3dB 迟滞/hold 独占/attack-release 平滑） | TestExpanderSlope / TestCompressorHardKnee |
| DY-04 | SC Listen 输出原始检源音频（±0.5 方波保持负半周，无整流 DC）；内部监听=输入直通 | TestSidechainListen |
| RV-01 | Brightness 就地更新 comb 阻尼（运行中 50→100 输出有差异且不清尾音） | TestReverbBrightness |
| MX-06 | EffectChain 按 (类型, instance_id) 匹配状态池；调序/删除不串状态（回声能量连续）；旁路实例 GR/检源清零 | TestEffectChainInstanceIdentity |
| DY-01 | 总电源只翻转实例级旁路（[False,True,False] 关→开不变）；预设保留自身模块状态（Flat Bypass 三模块保持 False）；Reset=产品默认值（模块关闭、总电源不变）；复合单元 Make Up 移到 dynamics 之后（Comp 旁路仍生效）；Mix 方向契约显式文档化（0=干/100=湿，未反转） | tests/shared/test_audio_dy01_dl01_contracts.py |
| DL-01 | Link 开关一次手势恰好一次 committed；输入菜单 = Stereo/Mono/Left/Right；旧 LCR/Quad/5.x 迁移为 Stereo（声音不变）；DSP 消费 Left/Right（L 冲激在双线出现、R 冲激不进网络）；Mono=(L+R)/2 反相相消锁定 | 同上 + TestDl01DspInputSelection |
| MX-04 | 同轨多 clip 先求和、轨 DSP 一次处理：实时 mix_samples 重构为 clip-stage 汇入轨输入块 → volume/panner/EQ/FX/mute/路由对求和块一次；导出循环经 pending 帧缓存 + suffix-min 提前 flush 同构重构（render_track_block 每帧一次）；压缩器检测合成信号、decoder 顺序无关 | tests/shared/test_audio_mx04_mx07_mx08_engine.py::TestMx04SumThenTrackDsp |
| MX-07 | `AudioEffect.tail_estimate_samples` 契约（Delay=延迟×反馈衰减圈数上限 4s；Reverb=2×RT60 上限 8s；Chain=启用实例最大值）；无 clip 尾音块喂零推进（60ms/100%wet 回声 ≈0.29 量级出现在 drain 帧窗）；Bus 同一 drain 契约；seek/stop 清调度；导出选区内 drain（不扩展选区长度——跨选区交付策略仍待办） | TestMx07TailDrain |
| MX-08 | 导出 render_track_block 传 sidechain_taps（原 E04 缺口）；render_bus_stage 增 sidechain_taps 参数；导出按 SC 依赖排序轨（环依赖告警+内部检源回退）；检源轨按帧缓存输出供被控轨/Bus 拼接 tap | TestMx08SidechainExportParity |
| MX-01 | stop/pause/seek 广播版本化完整静音事件（电平地板+GR/SC 清空+spectrum=None+代数）；`audio_telemetry_reset` 信号；面板代数守卫丢弃旧代排队事件；对话框清频谱/peak-hold/GR/输出表 | tests/shared/test_audio_mx01_telemetry_lifecycle.py |
| MX-02 | EffectChain.process 按实例真实采样 in/out 峰值 + GR/SC（单一真源）；旁路实例直通语义 + bypassed 标记；TrackLevel.taps 全链路下发；EQ/Dynamics/Delay/Reverb 表读各自实例 tap（多实例不串读数，Dynamics Input=EG 输入、Output=Lim 输出） | TestMx02PerInstanceTaps |
| MX-12 | strip.close_all_dialogs()（轨删除/工程切换关闭单例+多实例对话框）；deregister_track 先关对话框再拆除；Dynamics 单例 finished/destroyed 绑定方法清引用（GC 后 lambda 闭包 NameError 根治——该修复同时消除了既有闪失） | 同上 + 3x 稳定复跑 |
| MX-11 | `_capture_strip_state/_apply_strip_state` 统一 9 字段（volume/pan/mute/solo/EQ/FX 链/Panner/output_bus/sends）；ApplyChannelStripPresetCommand 与 ApplyMixSnapshotCommand apply/undo 同一字段集（旧 undo 漏 EQ/FX/routing 已修）；命令序列化往返（跨会话 undo）；ChannelStripPreset 捕获 panner（旧预设无 panner 键→保留目标轨现状） | tests/edit/test_audio_mx11_preset_snapshot.py |
| ST-03 | `audio_dsp.ALGORITHM_VERSION=2` + `ALGORITHM_CHANGELOG` 记录全部改声契约；预设落盘盖 algorithm_version；未知字段/坏字段降级不阻断（strip_eq/effect_chain/panner parse failed 警告）；Delay 旧枚举迁移（LCR/Quad/5.x→Stereo） | TestSt03PresetForwardCompat |
| MX-09 | 频谱环窗存立体声、按 write 指针重排（跨界无伪瞬态）；L/R 功率合成（L=-R 不相消）；`sample_rate_changed` 信号 → 面板 → strip → EQ 曲线/频谱轴（构造注入 + 运行更新）；采样率变化重置 DSP 状态 | TestMx09SpectrumCorrectness |
| MX-13 | 推子底端=硬零端点（db_to_linear(VOLUME_DB_MIN)=0.0 严格静音）；连续区 (−60,+6] 映射冻结（旧工程兼容）；volume=0 capture/apply 往返严格 0；0dB 直通（现有回归） | TestMx13FaderHardZero |
| DL-02 | 延迟环形缓冲按最大容量 1200ms 预分配（参数时间变化不重建不清历史）；分数读头（线性插值，60.5ms 冲激峰位误差 ≤1 采样）；负反馈极性交替、强反馈有界锁定 | TestDl02FractionalDelay |
| RV-04 | 几何变化不重建网络（拓扑固定常量；尺寸/距离只影响 ER 抽头与晚期偏移）；ring 只增不缩；采样率变化重置网络（防护语义保留）；拖尺寸/距离尾音不归零 | TestRv04GeometryHotUpdate |
| DL-03 | 尾音经 MX-07 契约成立（§1.3 后块 peak≈0.29 场景）；输出表经 MX-02 读实例 tap；tap_graph 参数图保持既有实现 | TestMx07TailDrain + TestMx02 |
| ST-01 | 五款对话框全部接入 `effect_preset` 用户库（~/.topos/effect_presets.json，原子写、单条损坏跳过、整库不可读拒绝覆写）；Dynamics/Delay/Reverb 预设条目、EQ（strip_eq）预设条目、Panner 完整 state 均跨会话加载；库不可读时对话框降级可用 | tests/shared/test_audio_mx01_telemetry_lifecycle.py（存储 3 项）+ tests/shared/test_audio_st01_eq_panner_presets.py（EQ/Panner 5 项） |

### 已实现（2026-11-15 续批：阶段 F/G/H）

| ID | 交付 | 证据 |
|---|---|---|
| MX-03 | Master 条带（`master_strip_eq`/`master_effect_chain` 序列化往返、坏字段降级、已知键）+ 实时/导出（configure_from_sequence→apply_master，音量后限幅器前）+ Master 命令（apply/undo/序列化）+ Master 遥测（"-1" 键，post-limiter，spectrum/GR/SC/taps）+ Master strip UI 信号/门控/回填闭环；Bus 遥测补 pre/GR/SC/taps/频谱/LUFS；**修复实时 Bus 链外部侧链被无声忽略**（MX-08 残留缺口）与 Bus/Master 引用检源 tap 不记录的 `_sidechain_track_order` 缺陷 | tests/edit/test_audio_mx03_master_bus.py（13 项） |
| MX-14 | Send 模型 v2：`tap`（post_fader 默认/pre_fader）+ `muted`（独立静音）；旧 dict/2 元组降级 post_fader（声音不变）；实时 pre-send 取 clip 求和块（自动化静音门控）、Bus send 同契约；导出 `summed` 为 pre 源；`unpack_send` 三形态兼容；muted send 不构成环边；**修复 UpdateSendCommand 重建丢字段**（改 level 会静默复位 tap/muted）；RoutingDialog Post/Pre 选择 + M 按钮 | tests/edit/test_audio_mx14_sends.py（16 项：推子 -12dB oracle 实时+离线、mute 隔离、旧工程兼容、命令往返、环边） |
| MX-15 | `layout_capability.py` 逐段能力矩阵（source_decode/clip_stage/track_dsp/bus_master/export；诚实边界：轨链 DSP 域=立体声、高度声道显式拒绝并给原因）；**修复 `from_av_layout_name` 把 "7.1.4" 静默当 7.1**（高度声道被无声丢弃）；全部支持组合×每源声道脉冲全扫（==矩阵列 + 时不变） | tests/edit/test_audio_mx15_layout_matrix.py（18 项） |
| MX-10 | `fx/{instance_id}/{param_key}` 自动化命名空间（模型 dict key 泛型，序列化/命令零改动）；`build_track_fx_sampler`（mode=off/无键/无链→None）；实时 `set_track_effect_chain(animated_sampler=)` 块边界应用；导出 `_TrackNode.fx_sampler` 同构；edit_page 同步重建；AutomationLane 枚举 fx 参数（registry schema 值域+保守回退；离散参数不开放曲线） | tests/edit/test_audio_mx10_fx_automation.py（9 项：回声跟随曲线、帧对齐块长等价 240/960、导出同构、实例隔离、undo） |
| MX-16 | LUFS 对照公开参考锁定（1kHz @-23dBFS 双单声道 → -23±0.1 LUFS；-20/-6 锚点）；满幅正弦 peak 0dBFS/RMS -3.01dBFS（引擎 TrackLevel + payload 换算）；-6dB 线性；反相立体声逐声道测量不相消；400ms 窗语义。True Peak（P2）未实施 | tests/edit/test_audio_mx16_meter_calibration.py（9 项） |
| EQ-02 | Voicing 带宽/增益耦合（自有可测模型：boost→Q 窄/cut→Q 宽，因子 10^(k·gain/60)，单调有界；Clean 与 0dB 恒直通=默认工程声音不变；仅 Bell/Shelf；ALGORITHM_VERSION=3）；每控件改频响锁；全形状×44.1/48/96k finite；0dB Bell 直通 | tests/edit/test_audio_eq02_eq03_refinement.py（12 项） |
| EQ-03 | 分析仪语义锁：1kHz 纯音定位正确 bin；静音贴地板；-12dB 增益后频谱随输出一致变化（分析点=轨链输出）；停止清频谱沿用 MX-01 | 同上（3 项） |
| DY-03 | 校准锁定：hold=100ms 释放起点 ≤1 采样（包络冻结 oracle）；tone-burst attack 收敛/释放衰减；48/96k 等时包络（≤0.5dB）；整块/随机分块 ≤1e-5（hold 0/100ms）；Limiter 阈值保护 + 跨块无增益跳回；端点 finite（attack=0 有定义）；Limiter 过冲遥测 `last_overshoot_db`（tap 暴露 overshoot_db） | tests/edit/test_audio_dy03_calibration.py（16 项） |
| RV-02 | 一阶+有限阶角 image-source ER（各维独立附加行程；同均值不同形状分布不同）；负 Pre Delay 传播模型 `max(0, pre+dist/c)`（抵消距离时延，不再整段钳 0）；Distance 只动 ER、晚期只随 pre_delay；对称权重+±2% 去相关（立体声对称契约保持）；首反射几何 oracle ≤1 采样 | tests/edit/test_audio_rv02_er_model.py（9 项）；dsp_pro 首反射契约更新为 image-source 物理值 |
| RV-03 | 8 线正交 FDN 替换双 Schroeder 组：Householder 反馈（谱半径≤max\|g\|<1，无啸叫/能量增长）、每线 RT60 校准增益、回路阻尼（brightness）、子块精确处理、L/R 互补线组去相关、全确定性；中低频段（100-1000Hz）RT60 校准 0.5/1/2/4s 实测 0.53/0.99/2.03/4.11s（±10%）；热更新语义保持；`_fdn_process` 双路径删除；**修复 3 项基线失败**（wd12g52 引用已删 `_combs`/旧参数名的陈旧测试重写为当前契约） | tests/edit/test_audio_rv03_fdn.py（11 项） |
| PAN-02 | `vbap.py` 离线域：2.0/5.1/7.1/7.1.4 标准摆位；2D 邻对/3D 三元组解（有效扇区+最小角距选择；能量归一；盲区最近扬声器回退）；扬声器方向→该扬声器增益最大；F/B 切换；高度进顶部对；无高度布局仰角钳水平面并 exact=False 明确限制；Spread 能量合成（宽度不改总能量）。实时轨域仍立体声（MX-15 矩阵）——离线路由已测、实机监听未测 | tests/edit/test_audio_pan02_pan03_vbap.py（32 项） |
| PAN-03 | `lfe_route`：Pre/Post/Only 显式语义（Only 只向 LFE；主声道不因 Boom 重复加倍；LFE 独立声道，downmix 默认不回混 L/R）；downmix LFE 隔离锁定。位置/pan 自动化沿用轨级（pan）与 MX-10（fx 参数）；Panner spread/rotate 参数自动化未接（fx 命名空间覆盖 effect_chain 实例，Panner 是轨级 dict——待 Panner 模型实例化） | 同上（LFE 6 项） |

### 核心已实现（边界明确）

| ID | 已交付 | 边界（明确待办） |
|---|---|---|
| MX-05 | 处理顺序固化为版本化契约 `TRACK_CHAIN_ORDER`/`ORDER_VERSION`（实时/离线同序）；Send tap 位置已版本化（MX-14：post_fader 默认/pre_fader 可选） | pre/post-FX 更细粒度 tap、可切换顺序组合、insert 总旁路语义——需 MX-05 图扩展 |
| MX-02 扩展 | 实例 in/out/GR/SC tap 全链路 | pre/post 可选切换 UI、queued/coalesced 刷新节流（当前 33Hz 节流已有）、tap DTO 版本化 |
| ST-02 | Delay/Reverb A/B bank 现状保留；快照走 MX-11 全字段 | A/B 是否持久化到工程的产品契约未决（保持窗口内语义），set_current_params 回填跨 bank 一致性未改 |
| PAN-02/03 | VBAP/LFE 离线数学域全量（见上表） | 实时轨域接入（轨链 DSP 为立体声域——MX-15 矩阵；VBAP 进实时需多声道轨域，实机监听未测）；Panner spread/rotate 参数自动化（Panner 为轨级 dict，非 effect_chain 实例，未入 fx 命名空间） |
| MX-18 | 代码路径审计（arm≠录音、solo 闭包、take 保留——见 §5 审计结论） | 真机端到端核验（真实输入设备/拔出/写盘失败实机行为、输入输出延迟与 pre-roll 校准）需硬件 |

### 未实施（依赖产品决策或 P2）

| ID | 说明 |
|---|---|
| MX-17 | Group/Link/VCA（P2）——计划 §12 明确"阶段I之后单独立项"，本次显式延期 |
| True Peak | MX-16 的 P2 可选增强（过采样滤波 + dBTP 单位）；现有 sample peak 不冒充 True Peak |
| MX-10 扩展 | 离散开关参数的事件式自动化（当前仅数值参数开放曲线）；Write/Touch/Latch 录制仍限 volume/pan/mute（fx 曲线为 Read 语义） |

## 3. 回归状态

- 新增 focused 回归（2026-11-14 首轮）：70+（阶段A 18 项 + DY-01/DL-01 11 项 + MX-04/07/08/02/09 引擎 15 项 + MX-01/13 生命周期 12 项 + MX-11/ST-03 6 项 + DL-02 4 项 + RV-04 3 项 + DY-03/EQ-02 锁 5 项 + ST-01 存储 3 项）。
- 新增 focused 回归（2026-11-15 续批）：149 项（ST-01 EQ/Panner 5 + MX-03 13 + MX-14 16 + MX-15 18 + DY-03 16 + RV-02 9 + MX-16 9 + EQ-02/03 12 + MX-10 9 + RV-03 11 + PAN-02/03 32）。
- 更新为旧缺陷契约的测试：B13 静音广播（→版本化 payload）、SC Listen 广播（→原始检源）、EQ 身份段数（→真 Butterworth 段数）、graph parity/mixer units 键集（→含 master "-1" 遥测条目）、wd17w2 路由快照（→4 元组 send）、dsp_pro 首反射（→image-source 物理值）、wd12g52 ReverbRebuild 3 项（→当前参数名与 FDN 网络，修复基线失败）。
- 宽回归基线（2026-11-14 轮）：15929 通过 / 30 失败，30 个失败经干净基线 worktree（df749759）复跑确认全部为既有问题。续批（2026-11-15）修复了其中音频域 3 项（wd12g52 ReverbRebuild）与 2 项（channel_layout_import 的 MagicMock 探测，经 stash 验证为环境相关既有失败，未计入修复）。
- 独立 Review（general-purpose agent，首轮）：3 Critical 已全部修复；剩余建议（Gate 逐样本循环优化、tap 采样免分配、导出侧链缓存滚动窗口、A3 懒分配恢复等）为性能卫生待办。
- 真实主窗播放/导出的端到端验证不在自动化范围内（计划 §13：合成信号侧已全覆盖，实播回归建议按 §13 测试工程清单执行一次）。

## 4. 遗留风险与建议（2026-11-15 更新）

1. **导出跨选区尾音**：当前导出只在选区内 drain；跨选区交付策略（允许扩展并显式告知）待产品决策。
2. **clip 级尾音**：同轨 clip A 结束、clip B 播放时，A 的 clip-FX 尾音不延续（track 级尾音已完整；clip 级需逐 clip 尾音调度）。
3. **Make Up 三实例形态**：轨级三实例存储下 makeup 随 comp 实例（位于 Lim 之前）；复合单元已 post-dynamics。两形态位置统一需 MX-05 图扩展。
4. **Mix 方向**：保留 Topos 现方向（0=干/100=湿）并显式文档化；如采用官方反向需 ST-03 迁移。
5. **MX-10 粒度**：fx 参数自动化为块边界粒度（与 clip FX 同型）；Delay 时间这类参数的块内斜变/交叉淡化未实现（拖动听感由 DSP 端既有平滑承担）。
6. **Master 尾音**：Master FX（Delay/Reverb）尾音依赖播放线程继续调用 mix_samples；时间线自然结束后 master 尾音与轨尾音同窗口耗尽，无独立 master drain 调度（stop 由 reset_dsp_states 清）。
7. **VBAP 实时接入**：PAN-02 离线数学域已验证；接入实时需多声道轨域（MX-15 立体声边界）——在此之前 Panner UI 的 5.1/7.1 舞台图仍为参数表达（不冒充多声道输出）。

## 5. MX-18 录音路径静态审计（2026-11-15；真机核验仍待硬件）

按计划 §5 MX-18 验收逐条判定（代码级证据，无硬件实机）：

| 验收条目 | 判定 | 要点 |
|---|---|---|
| arm 状态 ≠ 正在录音 | **部分具备** | R 按钮=立即开录/停录开关（controller 直连 start/stop）；无 armed 待命分层、无 transport Record 键；arm 不入工程模型（重开不保留）。RecordingManager 内部有 pre_roll/recording/stopping/flushing/failed 状态机与同轨重 arm 守卫 |
| 停止/取消/设备拔出安全收尾并保留 take | **具备（取消无 UI 入口为设计现状）** | 停止→post-roll→finalize→completed；设备拔出→save_partial（completed_partial，接受导入）；磁盘满/写盘失败→failed 状态+原子写（tmp+os.replace）；discard/cancel 仅测试调用 |
| 无意外反馈回授 | **部分具备** | 监听为 opt-in（ADR 面板勾选）；输入监听走独立 QAudioSink（不进 mixer 图，不受 solo/mute 影响——也无 duck/回路检测）；pre-roll 期间即转发 |
| solo 普通轨不截断必经 Bus | **已具备** | RMA-04 `compute_audible_solo_closure` 闭包（下游链+send 保留），实时/渲染器/stem/导出四路径消费（graph_parity TestSoloOracle 锁定） |
| 设备切换不重置工程 gain/routing | **具备** | 输出设备丢失仅 stop()；gain/routing 存于 timeline 模型，无设备事件驱动的重置路径；录音输入设备缺失回退默认设备（本次已补警告日志，不再静默） |

已修：R 按钮陈旧注释（"占位，录音路由待实现"→实际语义）；输入设备回退无日志。
明确缺口（产品决策待定，不在本轮修复）：arm 待命分层与工程持久化、监听 duck/回授检测、ADR input patch 不持久化、应用退出无录音会话全局清理。
