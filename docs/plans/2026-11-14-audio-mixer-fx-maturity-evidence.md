# 音频 Mixer 与五款效果器成熟化 — 实施证据与交付边界

- 日期：2026-11-14。
- 对应计划：[2026-11-14-audio-mixer-fx-maturity-plan.md](./2026-11-14-audio-mixer-fx-maturity-plan.md)。
- 代码基线：df749759 起的 10 个提交（见文末提交清单）。
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

### 核心已实现（边界明确）

| ID | 已交付 | 边界（明确待办） |
|---|---|---|
| MX-05 | 处理顺序固化为版本化契约 `TRACK_CHAIN_ORDER`/`ORDER_VERSION`（实时/离线同序，见 audio_playback.py 头注）；Send tap 位置现状=post-fader | pre-send/pre-FX tap 位置、可切换顺序组合、insert 总旁路语义——需 Send 模型扩展（MX-14）后开放 |
| MX-02 扩展 | 实例 in/out/GR/SC tap 全链路 | pre/post 可选切换 UI、queued/coalesced 刷新节流（当前 33Hz 节流已有）、tap DTO 版本化 |
| ST-02 | Delay/Reverb A/B bank 现状保留；快照走 MX-11 全字段 | A/B 是否持久化到工程的产品契约未决（保持窗口内语义），set_current_params 回填跨 bank 一致性未改 |
| MX-15 | adapt_channels/SourcePatch 既有能力保留；导出/实时均声明容器布局 | source→track→bus→export 全布局能力矩阵、5.1/7.1 逐声道脉冲验证、Panner 2×2 之外的矩阵——H 阶段依赖 |
| DY-03 | 时间常数统一 RC 定义（`_time_to_coeff`）；hold 路径线性域冻结；检测域单一 | Hold=100ms 释放起点 ≤1 采样、tone-burst 响应、Limiter 过冲报告未测（待 G 阶段校准任务） |
| RV-02 | Pre-Delay 范围 ±200ms 保留（不再整段钳 0 的语义保持在参数层） | 几何 image-source ER、负 pre-delay 抵消传播时延、Distance 只动 ER——未实施（需 H 阶段） |
| MX-16 | Peak/RMS/LUFS 测量链保留 + LUFS 后端探测降级（不伪造读数） | 参考文件校准、clipping 计数、相关度表、True Peak（可选增强）未实施 |

### 未实施（依赖阶段 F/G/H/I，或可选增强）

| ID | 说明 |
|---|---|
| MX-03 | Master/Bus 专属 EQ/FX/参数模型与命令——普通轨/Bus 的 EQ/FX/遥测已可用（E05/E02），Master 参数化处理需独立模型（计划阶段 F） |
| MX-10 | 效果参数自动化（instance_id 路径、时间戳控制消息）——轨级 volume/pan/mute 自动化已有，FX 参数自动化需 schema 扩展（阶段 G） |
| MX-14 | Send tap 位置/独立 mute/自动化地址（Send 模型仍是 post-fader 三字段） |
| MX-17 | Group/Link/VCA（可选 P2，核心 Mixer 正确后再开放） |
| MX-18 | 录音/设备生命周期端到端核验（需要真实硬件输入；Record Arm 能力开关现状保留） |
| EQ-02 | Voicing 模型可测目标（带宽/增益耦合）与多 Solo 旁路规则细化（Solo 现状=只应用 Solo 频段，DSP/UI 同一规则） |
| EQ-03 | pre/post 双曲线、SPEC/PEAK 开关语义细化——peak hold reset 已随 MX-01 交付 |
| PAN-02/03 | VBAP 多扬声器/真实 LFE/位置自动化（H 阶段；Panner 立体声矩阵数学已修正确） |
| RV-03 | 校准 Schroeder→8 线 FDN、分频段 RT60、种子化调制（H 阶段；当前 4 comb+2 allpass 方案保留且 Brightness/RT60/几何热更新已正确） |

## 3. 回归状态

- 新增 focused 回归：70+（阶段A 18 项 + DY-01/DL-01 11 项 + MX-04/07/08/02/09 引擎 15 项 + MX-01/13 生命周期 12 项 + MX-11/ST-03 6 项 + DL-02 4 项 + RV-04 3 项 + DY-03/EQ-02 锁 5 项 + ST-01 存储 3 项）。
- 更新为旧缺陷契约的测试：B13 静音广播（→版本化 payload）、SC Listen 广播（→原始检源）、EQ 身份段数（→真 Butterworth 段数）。
- 音频域全量：`tests/edit` + `tests/shared` 音频相关套件 452+ 通过。
- 宽回归（tests/edit + tests/shared 全量）：**15929 通过 / 30 失败**；经干净基线
  worktree（df749759）抽样复跑确认，30 个失败全部为既有问题（effect_catalog 契约、
  n03 keyer/mask、timeline 手势/指针、ReverbRebuild 引用不存在的 `_combs` 旧属性、
  ef01 参数缓存计数等），与本次改动无关——本次改动零新增回归。
- 独立 Review（general-purpose agent）发现 3 Critical + 10 Suggestion/Nice：
  3 Critical 已全部修复（reverb 用户预设丢失回填、静音 payload lufs 类型、
  旧版预设清空 Panner/路由），并采纳 3 项建议（UI 多 Solo 对齐 DSP、实时
  载荷盖播放代、效果预设盖算法版本）；审查同时确认分数读头越界、ring 回绕、
  导出 flush 帧序、链身份往返、锁边界均正确。剩余建议（Gate 逐样本循环
  优化、tap 采样免分配、导出侧链缓存滚动窗口、A3 懒分配恢复等）记录为
  性能卫生待办，不阻塞正确性。
- 真实主窗播放/导出的端到端验证不在本次自动化范围内（计划 §13 要求真实视频
  + 合成信号互补；合成信号侧已全部覆盖，集成侧建议按 §13 测试工程清单跑一次
  实播回归）。

## 4. 遗留风险与建议

1. **导出跨选区尾音**：当前导出只在选区内 drain；跨选区交付策略（允许扩展并显式告知）待产品决策。
2. **混合布局轨 pan 法则**：导出按轨内首 clip 布局选法则（同布局轨与实时一致；混合布局为显式近似，MX-15 统一后消除）。
3. **clip 级尾音**：同轨 clip A 结束、clip B 播放时，A 的 clip-FX 尾音不延续（track 级尾音已完整；clip 级需逐 clip 尾音调度）。
4. **Make Up 三实例形态**：轨级三实例存储下 makeup 随 comp 实例（位于 Lim 之前）；复合单元已 post-dynamics。两形态位置统一需 MX-05 图扩展。
5. **Mix 方向**：保留 Topos 现方向（0=干/100=湿）并显式文档化；如采用官方反向需 ST-03 迁移（v2→v3）。
