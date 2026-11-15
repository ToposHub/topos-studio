# Audio Mixer 与效果器：实施后复审及新增效果器接入计划

- 复审日期：2026-11-15。
- 输入基线：`06d670a9`；对照 [原 38 项计划](2026-11-14-audio-mixer-fx-maturity-plan.md) 与 [原实施证据](2026-11-14-audio-mixer-fx-maturity-evidence.md)。本次局部修复位于工作区。
- 范围：EQ、Dynamics、Panner、Delay、Reverb；普通轨、Bus、Master；实时播放、成片导出、Stem、自动化、预设／快照、工程兼容、UI 生命周期。
- 结论：已有大量有效实现，普通音轨五窗真实集成正常，但还不满足原计划的最终完成门槛。主要阻塞是离线调度、跨路径一致性和状态兼容；不能把数学辅助函数、版本常量或单项测试通过等同于整条生产路径已验收。
- 本文采用业界公开、成熟的实现思路，不要求复制 Fairlight 专有算法或逐像素外观。未进行新的 Resolve 声学 A/B，不宣称音色完全相同。
- 本次直接修复可局部验证的问题；下列结构性问题留给后续实施，不在本次重写音频图、工程 schema 或录音系统。
- 新增范围（2026-11-15 补充）：§6–§10 为用户提供的 Voice Changer（变声器）与 Spectral Mangler（奇幻畸变器）的前端／后端接入方案，**尚未实现**。§1–§5 的五款复审结果、1595 项通过和真实播放证据不包含这两个新效果；原 R01–R13 编号保持不变。

## 1. 证据、验证范围与结果

证据目录：[docs/audit/2026-11-15-audio-mixer-review](../audit/2026-11-15-audio-mixer-review/README.md)。

1. 复审前新增成熟化测试 254 项通过；较宽音频回归 1491 通过、1 跳过。独立探针仍复现本报告中的缺陷，说明测试覆盖边界需要继续补足。
2. 最终 **82 个相关测试文件：1595 通过、1 跳过，74.15s**；本轮四个新回归文件共 57 项通过。文件清单和 JUnit 见证据目录；包含 Master 自动化／限幅、播放、导出、Stem、录音相关回归。这不是全仓测试结果。
3. 在完整主窗口中新建视频 V1／关联 A1，用用户提供的 `tests/test_videos/2K_h264.mp4` 实际播放约 12 秒，打开五窗：收到 203 条遥测，EQ 96 bins，Dynamics／Delay／Reverb 电平变化；暂停后表计／频谱归零，恢复播放新增 25 条遥测。Panner 舞台是参数图，不要求持续改变几何形状。
4. 该次 ring 统计 underruns=0、overflows=0、total_dropped=0；mix_samples 测量 p50=9.15ms、p99=74.25ms、max=83.04ms，n=345。能播放不代表已满足实时负载预算；启动／恢复／稳态尚未分组统计，详见 R08。
5. 合成探针使用真实 TimelineExporter、AudioGraphRenderer、AudioMixer 和模型／命令；仅在音源和编码输出边界使用确定性输入或替身。大问题的当前数值在 [remaining-issues.json](../audit/2026-11-15-audio-mixer-review/remaining-issues.json)，可用旁边脚本重新测量。

未作为“已验收”处理的范围：多声道实机监听、输入设备热拔插／录音磁盘故障、长工程峰值内存、取消和应用自然退出的完整资源收尾、与 Resolve 的盲听／逐参数声学对照。真实主窗探针结束采用测试进程强制退出，不能据此证明自然退出清理正确。

## 2. 本次已直接修复

| 编号 | 问题与原行为 | 最终改动 | 验证 |
|---|---|---|---|
| F01 | 播放进入自然空段时缓存仍保留最后一条非空电平，效果窗冻结 | 区分“新测量为空”和“没有新 mix”；保留 30ms 节流；消失的轨／Master 发完整静音事件 | 五窗真实 thread→manager→panel；prefill／direct 两路；部分轨结束；ring-only 不误清 |
| F02 | 先协商采样率再新增轨，轨与 EQ 构造仍用默认 48kHz | Panel 缓存引擎采样率，新 strip 继承；EQ 图构造立即使用注入采样率 | 44.1／96kHz 曲线对独立 DSP 响应 |
| F03 | Dynamics 机架开关修改模块开关，破坏三模块组合；菜单重复操作组内单实例；EQ／FX 徽章忽略实例旁路 | 总开关只改实例级 enabled，模块状态保持；同步已开窗口；徽章检查两级开关；统一右键组语义 | `[False,True,False]` 关开恢复，一次提交；EQ／DY／FX 均不把旁路实例显示为激活 |
| F04 | 对话框 close 后 Qt 子对象滞留；旧 Dynamics 窗延迟销毁可能清掉新引用 | 共用 DeleteOnClose 和按对象身份清理注册表 | 12 个入口各连续开关 3 次，旧窗销毁不影响新窗 |
| F05 | 大房间 ER 路径超出按平均尺寸估算的 ring，反射取模提前出现 | 按实际 ER／late／调制读头尺寸分配，包含整个输入块与插值余量 | 长路径不提前；整块／256／2048／随机分块一致 |
| F06 | Reverb 扩容清空历史，播放／导出换块长或热改尺寸会丢 ER | 扩容按时间顺序迁移 ring 历史；采样率切换仍显式 reset | 预热后扩块／几何增容对照；已存历史保留 |
| F07 | 复合 Dynamics 仅有 Make Up 时 is_active=False，链跳过有效增益 | 总电源开且非零 Make Up 也视为有效 | 模块全旁路、+6dB 仍为约 1.995 倍；总旁路直通 |
| F08 | params.enabled=False 时仍可读到上一块 GR／SC | 采集同时检查实例总旁路与内部 active | 立即查询／下一块均清除陈旧 GR／检源 |
| F09 | 串联尾音取 max，两个 80ms 全湿 Delay 仅给 80ms drain，160ms 回声消失 | 串联保守求和；无效／非有限估计不进入预算 | 实际 AudioMixer 在 decoder=None 后保留 160ms 回声 |
| F10 | 响度归一化保护再次 apply_master，重复 EQ／压缩／Delay／Reverb | 新增仅限幅入口；独立归一化 pass 重置 limiter 包络 | 0dB normalize 导出 PCM 与未 normalize 相同，200ms Delay 不变 400ms |
| F11 | 仅 Master 引用的外部 SC 源未纳入导出缓存，退回内部检源 | 检源集合包括 Master 链所需 source | 真实 WAV／TimelineExporter 与显式 SC oracle 对齐 |
| F12 | Master 静音后既有 Delay／Reverb 仍从有状态网络输出 | 最终静音门控位于用户 FX 后、保护限幅前；自动化 envelope 只乘一次 | 实时静态／自动化 mute；离线自动化 mute 都截住既有尾音 |
| F13 | Master TrackLevel.pre_peak 默认 0 | 在 Master 增益前采真实输入峰值 | 输入 0.2、Master 0.5，pre=0.2、post=0.1 |
| F14 | 原测试有弱断言／空断言，旧窗口替身只有 QObject 接口 | 60.51ms 真正覆盖分数采样；负反馈在同一全局时间轴检查且强制两个回声存在；1kHz 频谱改为最近 bin ±1；窗口替身保留 QDialog 生命周期契约 | 修订后的原测试和完整音频回归通过 |

新回归文件：

- `tests/edit/test_audio_review_followup_dsp.py`
- `tests/edit/test_audio_review_followup_export.py`
- `tests/shared/test_audio_review_followup_telemetry.py`
- `tests/shared/test_audio_review_followup_ui.py`

这些修复不包含 R01–R13 的大改动。例如 F09 只修串联预算，不代表 Master／clip 尾音和离线时序均已完成；F11 修 Master 源收集，不代表跨 Bus 侧链调度或内存已解决。

## 3. 原 38 项逐项复核

“已有覆盖”表示检查到实现及相关测试通过，并非所有声音、设备、失败路径已穷尽。“部分”表示原任务至少有一项关键验收仍缺失。

| 原任务 | 本次判定 | 已核实内容／剩余关联任务 |
|---|---|---|
| MX-01 遥测生命周期 | 小修后已有覆盖 | play／pause／resume、自然空段补齐；长时间和设备重建见 R10 |
| MX-02 实例／阶段读数 | 部分 | 实例 tap 已实现，F08／F13 补齐；频谱当前为轨输出，非所有实例各有独立分析点；tap 单位／旁路语义见 R11 |
| MX-03 Master／Bus | 部分 | Master EQ／FX 和遥测可运行；F10–F13；Bus 自动化、Master 尾音／快照仍缺，见 R02／R04／R05 |
| MX-04 求和后单次 DSP | 部分 | 实时普通轨有求和；导出重复推进旧帧、Stem 仍逐 clip 进轨 DSP，见 R02／R03 |
| MX-05 处理顺序／tap | 部分 | 固定链顺序和 Send tap 已声明；两形态 Make Up 不同，细粒度 tap／可切顺序仍缺，见 R09／R11 |
| MX-06 实例身份／旁路 | 小修后已有覆盖 | 按 id 保留状态；F08 清两级旁路读数；模型外部替换到开窗回填见 R06 |
| MX-07 尾音／延迟／平滑 | 部分 | F09 串联预算；clip／Master drain、非线性参数过渡和显式延迟补偿见 R02／R04／R07 |
| MX-08 侧链同构 | 部分 | F11 修 Master-only 源；Bus 依赖调度、Stem tap、导出全长缓存见 R02／R03 |
| MX-09 FFT／采样率 | 部分 | 立体声功率、环窗、F02；设备回退未完整通知 manager／UI，见 R10 |
| MX-10 自动化 | 部分 | 普通轨数值 FX 块边界采样；Bus sampler 未消费；Master 无同等 FX 通路，见 R05／R07 |
| MX-11 预设／快照 | 部分 | 非空条带状态和命令 undo 有覆盖；空值清除与 Master EQ／FX 快照未完成，见 R05 |
| MX-12 生命周期／诊断 | 部分 | F04 修关窗释放；外部回填／删除后迟到提交、失败诊断见 R01／R06／R11 |
| MX-13 推子零点／单位 | 已有覆盖 | 最底端硬零；连续区和旧工程映射保留；未发现需要本轮改动的数值问题 |
| MX-14 Send | 部分 | 主播放／成片的 pre/post 和 mute 有测试；Stem 忽略 tap，见 R03 |
| MX-15 布局／Patch | 部分 | 布局矩阵／能力函数存在；实时轨域仍 stereo，多声道 Panner 未接主图，见 R12 |
| MX-16 表计／监听 | 部分 | sample peak／RMS／LUFS 校准有覆盖；无独立 dBTP 能力；真实监听链见 R10／R11 |
| MX-17 Group／VCA | 显式延期 | P2，可在基础正确性完成后实施，见 R13 |
| MX-18 录音／设备 | 部分／需硬件 | R 按钮直接录音而非 arm；监听绕独立 sink；真机失败收尾、退出清理见 R10 |
| EQ-01 系数／响应 | 已有覆盖，但兼容未完成 | Butterworth 斜率、Shelf Q、共享响应；旧算法声音变化需 R01 |
| EQ-02 六段／Solo／Voicing | 已有覆盖，但能力口径需保留 | 自有带宽耦合模型、Solo 有测试；不声明 Fairlight 专有 Voicing 完全一致 |
| EQ-03 分析仪 | 小修后已有覆盖 | F14 收紧 bin 判据；静音／暂停清谱；每实例分析位置见 R11 |
| DY-01 旁路／Make Up／Mix | 部分 | F03／F07；复合与三实例 Make Up 位置不同，见 R09 |
| DY-02 静态曲线／Gate | 已有覆盖 | 下扩、knee、迟滞／hold 状态机；边界／长跑仍纳入 R08 压测 |
| DY-03 时间／Limiter | 已有覆盖但性能待验 | 48／96k 包络、hold／块一致、过冲报告已有；sample limiter 不等于 true peak，见 R08／R13 |
| DY-04 SC／Listen | 部分 | 原始检源保留极性、模块读数；图级 source 时序与回退见 R02／R03／R11 |
| PAN-01 stereo 矩阵 | 已有覆盖 | 两输出读取原输入、镜像与增益约束 |
| PAN-02 多扬声器 | 仅离线数学辅助已验 | VBAP helper 未接实时／导出 Panner 主处理域，见 R12 |
| PAN-03 LFE／位置自动化 | 部分 | LFE helper 单元测试；真实输出通道与 spread／rotate 自动化未贯通，见 R05／R12 |
| DL-01 输入／Link | 已有覆盖 | Stereo／Mono／L／R、单次提交；旧枚举含义需 R01 固化迁移 |
| DL-02 分数延迟／反馈／平滑 | 部分 | F14 修测试；静态分数读头／负反馈有效，热改时间不平滑且近零热点，见 R07／R08 |
| DL-03 尾音／图 | 部分 | F09；clip／Master、导出间隔仍有问题，见 R02／R04 |
| RV-01 Brightness | 已有覆盖 | 热改反馈阻尼、保留尾音；极端几何另外由 F05／F06 覆盖 |
| RV-02 ER／传播 | 小修后已有覆盖 | image-source／负 predelay／distance；F05 防长路径 wrap，整块／分块对齐 |
| RV-03 FDN／RT60 | stereo 算法已有覆盖 | 8 线反馈、RT60／finite／块一致有测试；真实多声道输出见 R12 |
| RV-04 热更新／三路输出 | 部分 | F06 修扩容失忆；参数连续性与全链尾音仍见 R04／R07 |
| ST-01 用户预设 | 部分 | 五窗用户库／原子存储有测试；未知数据保留／版本分发未完成，见 R01／R05 |
| ST-02 A/B／回填 | 部分 | Delay／Reverb 窗口内 A/B；外部 undo／replace 回填一致性不足，见 R06 |
| ST-03 迁移／故障 | 未达到原验收 | algorithm_version 仅标签，未知 FX 会抛错或整链丢弃；见 R01，作为发布阻塞 |

## 4. 后续任务总表与顺序

P0 表示可能改变交付声音或丢工程内容；P1 表示核心能力／稳定性；P2 为扩展。优先级不代表本次发现了安全事件。

| ID | 优先级 | 工作项 | 前置／交付 |
|---|---|---|---|
| R01 | P0 | 真实算法版本、未知数据保留与故障能力契约 | 先冻结旧 fixture，后续改声任务依赖 |
| R02 | P0 | 单调时间的共享音频图调度与有界 SC tap | R01；成片／实时采用一致节点推进规则 |
| R03 | P0 | Stem 复用同一调度、正确 pre-send／求和／SC | R02 接口；避免继续手抄第三套路由 |
| R04 | P0／P1 | clip／track／Bus／Master 完整尾音与范围策略 | R02；定义延迟／尾音／结束策略 |
| R05 | P0／P1 | 全状态 DTO、空值清除、Master 快照、全域自动化 | R01 schema；R02 采样入口 |
| R06 | P1 | 外部状态回填、编辑器身份与 A/B 契约 | R05 的唯一状态源 |
| R07 | P1 | 参数平滑／Delay 双读头过渡 | R01 版本；R02 样本时间 |
| R08 | P1 | 近零 Delay 热点与实时负载预算 | 先测再改；R07 不能新增失控热点 |
| R09 | P1 | Dynamics 两种存储形态处理顺序统一 | R01／R02，禁止静默移动旧 Make Up |
| R10 | P1 | 设备采样率／录音／监听／自然退出 | 与模型保持解耦；硬件验收 |
| R11 | P1 | 遥测 tap／单位／旁路与故障诊断 | R02／R05 |
| R12 | P1／P2 | 真实多声道图、Panner／LFE 主路径接入 | R01／R02，硬件或逐通道离线验证 |
| R13 | P2 | VCA／分组、True Peak、事件自动化等 | 基础 P0 完成后独立立项 |

推荐先做 R01，再 R02→R03／R04；R05 的状态 DTO 可与图接口设计并行，但同一字段和时序契约只定义一次。R06–R11 按接口收敛分批完成。多声道不应先扩 UI 后补信号图。

## 5. 详细实施与验收

### R01：版本兼容、未知数据与能力声明

**证据／影响：** `audio_dsp.ALGORITHM_VERSION` 已到 3，但 `mixer_preset.ChannelStripPreset.from_dict` 不保存／分发该版本，`to_dict` 总写当前值。探针将 777 读入再保存后变成 3。普通轨未知 `future_fx` 导致 `EditTrack.from_dict` 抛错；Master 捕获错误后把整条链设为 None。字段没有保留下来，不能称为前向兼容。对旧 `df749759` 和当前 HPF（1000Hz／12dB，250Hz 正弦）独立测量稳态 RMS 相差约 24.12dB，旧工程默认声音已改变。

**位置：** `core/audio_dsp.py`、`core/timeline_v2.py` 的 StripEQParams／TrackEffectInstance／EffectChainParams／TimelineSequence.from_dict，`core/mixer_preset.py`、`core/effect_preset.py`，以及实际工程存取入口。

**实施步骤：**

1. 冻结改动前工程／preset／命令日志与参考 PCM；列出 slope、Shelf、Voicing、Dynamics slope／Make Up、Reverb 网络、Delay 输入枚举每次改声边界。
2. 在序列／实例／预设持有 schema_version 和必要的 algorithm_version；旧数据“没有版本”映射明确 legacy 版本，不直接解释成最新版。
3. 对有历史声音要求的算法保留兼容处理器或明确一次性升级；升级走 command，支持 undo，保存后可重开。采用兼容处理器是推荐方案，不能只改一个版本数字。
4. 未知 effect／参数／实例 metadata 使用 opaque payload 原样存回；加载可显示不支持占位，其他已知效果继续保留。单个不支持实例不得清整条链。
5. Registry 根据后端／布局／版本给出 supported、unsupported、degraded 及原因；preview 和 export 使用同一判断。用户明确选择降级后才旁路，不静默吞错误。
6. 增加迁移记录、未来版本拒绝覆写或无损往返；预设、快照、工程和命令日志统一规则。

**验证：** legacy／current／future fixture 各自 load→save→reload 字段无损；未知实例夹在两个 Delay 中间，已知两实例及 metadata 不丢；777 不变 3；损坏一实例保留原始数据并有诊断；旧 HPF PCM 在容差内重现；显式升级改变声音且 undo／重开恢复。不能用“没有异常”代替无损验证。

### R02：单调调度、有界侧链与路径一致性

**证据／影响：** 两片段位于帧 0..1、20..21，48kHz／25fps，第一个为脉冲、第二个静音；200ms 全湿 Delay。实际 `render_track_block` 调用 `[0,1,0,1,2,…,27]`，回声在 120ms 到达。early flush 已推进 DSP，final drain 又从旧帧推进，状态时间和写入位置分离。侧链源按帧缓存并在结束拼整段，1 秒 stereo float32 可同时有 384000B frame cache 与 384000B concat；等比例到 1 小时约 2.575GiB／源的额外 tap 副本，8 源约 20.6GiB，尚未计输出和解码缓存。该数值为结构性外推，不是实际分配 8 小时测试。

**位置：** `core/timeline_export.py::_render_audio_master` 的 pending／flush／drain／sc_frame_outputs；`core/audio_graph_renderer.py` 的轨／Bus阶段；`core/audio_playback.py` 的普通轨与 Bus 分阶段推进。

**实施步骤：**

1. 冻结上述失败工程，额外加入重叠 clip、乱序 clip、不同轨顺序、短块、选区非零起点和空隙。
2. 以绝对 sample cursor 为主时间，每个 block 完成所有轨 clip 求和、节点处理、Bus、Master 后才能推进下一块；同一 node 同一 cursor 只 process 一次。过渡期至少维护每节点 last_processed／next_sample，禁止回退和重复 drain。
3. 统一编译 output／send／SC 依赖，区分主音频边和检源边。定义 Bus→Track、Bus→Bus 的合法时序；循环应拒绝或显式带一块延迟并报告，不能靠没命中 tap 后改用内部检源假装正确。
4. tap 生命周期限定当前块及明确的延迟补偿窗口；用引用计数／消费者计数释放，不保留整部影片每源的副本。不只把 concat 换成 list，frame cache 也必须有界。
5. 解码可预取，但 DSP 按 cursor 推进；相同 clip 乱序 decode 不得改变声音。实时、成片和 Stem 共享调度接口及自动化时间坐标。
6. 分块写出／响度扫描按可重放输入设计；先解决时序和内存再优化分配。失败／取消释放 decoder 和所有未消费 tap。

**验证：** 200ms 回声到达误差≤1采样；每节点 cursor 严格递增，无重复；整块／随机块及不同 clip 存储顺序输出≤1e-5；Track／Bus／Master 侧链与显式 PCM oracle 对齐。长工程增加时长时 tap 活跃字节保持按 block×源数有界；记录进程 peak RSS、取消释放，不以 NumPy 单数组大小代替进程测量。

**接口骨架（设计示意，不直接作为实时内核）：**

```python
for block in sample_clock.blocks(start_sample, end_sample):
    taps.begin(block.start, block.count)
    for node in graph.topological_nodes:
        audio = inputs.sum_at(node, block)  # 无 clip 时可取预分配零块
        controls = automation.sample(node, block)
        result = node.process_once(block, audio, controls, taps.for_node(node))
        router.route(node, result, block, taps)
    writer.write(graph.master_output)
    taps.release_consumed()
```

不能把已经处理完的旧帧再次喂入 stateful node；seek 是显式 reset／重建过程，与顺序 drain 区别处理。

### R03：Stem 真正复用图

**证据／影响：** `core/stem_export.py::_iter_stem_pcm_chunks` 仍逐 clip 调 `render_track_block`；Send 解包为 `_tap` 后忽略，始终取 post-fader proc。输入常数 0.2、推子 0、unity pre-send 到 Bus：Master Stem 实测 0，正确为 0.2。多 clip 非线性求和、空段尾音和外部 SC 也不能仅凭简单三方 parity 测试宣布一致。

**实施步骤：**

1. 把 Stem 改为 R02 graph 的输出选择器／tap，而非重新实现 clip／track／send 循环。
2. 精确定义 TRACK／BUS／GROUP／MASTER stem 的 tap 位置和 include_master；独奏可听闭包和 SC 检源闭包分开，非可听 source 仍可供检源。
3. pre_send 从同一 pre-fader stage 拿数据，独立 mute／level 与成片一致；carry 同时保留路由目标及 tap 时间，不只缓存 post proc。
4. 同轨先求和再过非线性 DSP；共享 clip／track／Bus／Master 零输入 drain、自动化和延迟补偿。
5. 保留现有流式 WAV writer／取消／临时文件提交规则；避免把已经流式的 Stem 改回全长数组。

**验证：** 当前 pre-send 探针输出 0.2；post-send 推子 0 输出 0；send mute 只切该 send；两重叠 clip 过压缩器与 sum-first oracle 相同；间隔 Delay、Bus SC、Master FX／automation 在成片、Stem、实时捕获 PCM 对齐后≤1e-5。非线性 Master 下各 stems 直接相加不必等于成片，manifest 必须说明重组条件。

### R04：尾音、延迟与时间线结束策略

**证据／范围：** F09 已修串联 max→sum；原证据承认 clip 结束后 clip-FX 尾音不延续，Master 没有独立 drain，序列／选区结束默认截断。Delay 每实例 4s、Reverb 8s 估计上限只是工作预算，不能代表所有设置实际自然衰减到目标地板。

**实施步骤：**

1. 分离 latency_samples、tail_budget_samples、tail_active／energy；求和用于串联，max 用于并联汇合，不与时间补偿混用。
2. clip FX 在片段结束后以独立身份和源时间继续喂零；其输出汇入轨，轨／Bus／Master 的尾音预算在真正输入结束后接续。不能每个零块都重置 tail_left。
3. Master 即使所有轨已空，仍按自身网络状态排空；空输出块不能立即判 tail inactive（长 Delay 首回声前可持续为零）。
4. 明确 play 到结尾、stop、pause、seek、循环、静音／solo 对尾音的策略；seek 清旧历史，暂停是否冻结状态由产品契约决定。
5. 导出提供“严格选区”与“包含效果尾音”等明确模式，输出时长／进度和视频末尾延展策略同步；硬上限截尾时报告原因。
6. 增加有 latency 节点后在汇合点补齐干／湿／SC／音视频时序，缓存上限受控。

**验证：** clip→track→Bus→Master 各串一个已知 Delay 的首回声时间为其串联延迟和；无 clip 的多个零块后仍能听到回声；Reverb 长尾的 EDC 达到约定地板；严格范围输出长度不变，含尾模式长度／标识正确；seek 后旧尾为零；stop 后全部遥测归零。

### R05：状态 DTO、清空语义、Master 快照与自动化

**确认问题：**

- 干净轨的 ChannelStripPreset 经存盘再应用到富状态轨后，旧 FX、Panner、Bus 路由和 Send 全部保留。当前用 None／空值同时表达“旧版没存”和“明确清空”，无法区分。
- MixSnapshot 只保存 Master volume／mute。捕获 A 后把 Master EQ gain 改 9dB，再应用 A，仍为 9dB；Master FX 同样缺失。
- Bus 建立了 fx_sampler 但实时 Bus loop 和 render_bus_stage 不消费。`fx/D/level_db=-12`、输入 0.2，实时／离线均 0.2，正确约 0.050238。Master 和轨级 Panner 独立 dict 也未接同等参数自动化。

**实施步骤：**

1. 引入可序列化的字段存在性：缺字段=保留旧行为，显式 null／空列表／空 dict=清除；schema v2 preset 加 included_fields 或等价 patch DTO。不能简单把所有 None 都清除，旧预设仍需兼容。
2. capture／apply／undo／command roundtrip 使用同一 StripState／MasterState 定义；Master EQ／FX／未来版本／routing 进入快照。应用后的 revision／引擎同步同样走 command。
3. 全量 snapshot 明确每字段均在范围内，增量 preset 只改声明字段；SC 引用按稳定 id／符号策略解析，缺源不误绑同名轨。
4. R02 在普通轨、Bus、Master 每块统一消费自动化；Master／Panner 参数有稳定命名空间，不把轨级 Panner 偷塞进不存在的 FX 实例。
5. 对数值参数区分 ramp／块步进，对 enabled／mode 等离散参数使用事件；删除实例清 lane 或保留可诊断 orphan，id 不复用。
6. 支持范围写明 Read 与 Write／Touch／Latch，未实现录制的 FX lane 不宣称已经可写入手势。

**验证：** 旧五字段预设保留目标 routing；新全量空预设确实清空 FX／EQ／Panner／Bus／Sends；完整 A→B→A 与 undo／redo／保存重开逐字段等价；Master EQ／Delay 也恢复；Bus -12dB 探针过关；帧率 23.976／29.97／59.94、非零选区、随机块大小时自动化取样点按同一绝对样本时间。

### R06：编辑器回填、实例身份与 A/B

**证据／影响：** 已开 Delay=100ms，外部 set_effect_chain 同 id 改 800ms 后窗口仍 100ms。EQ 开窗 gain=9，外部链清空后窗口仍保留 9；旧窗再提交可把已移除效果重新写回。F04 的释放修复不解决仍打开窗口的模型真源问题。

**实施步骤：**

1. 每窗绑定 track_id／instance_id／model revision，不依赖创建窗口时捕获的旧实例 payload；提交时查当前模型存在性。
2. 统一外部状态通知：undo／redo、preset、snapshot、工程替换、删除、旁路、类型变化；同 id 回填全部相关控件和图，id 删除／类型变化则关闭或禁用窗口。
3. 回填使用信号锁／blocker，禁止程序化 setValue 产生 committed；revision 过期手势按策略取消或重放可审查 patch。
4. A/B 明确为 session audition 或工程字段；session 模式关闭丢失须有一致契约。外部 undo 回填不覆盖非活动 bank；需要持久化则进入 R05 DTO 和 command。
5. 轨删除／工程切换取消 queued preview 和迟到 commit；连接与订阅由窗口生命周期拥有。

**验证：** 开五窗分别执行 undo／redo／preset／snapshot／同 id replace／删除；UI、模型、DSP 一致；程序回填零提交；删除后旧窗不能复活效果；A→B→A、复制到非活动 bank、undo 后切 bank 保持各自内容；100 次开关后无 Qt 子对象／订阅增长。

### R07：Delay 与通用参数连续变化

**证据：** 48kHz、1kHz、振幅 0.5，Delay 10→10.25ms：边界跳变约 0.4364，稳态相邻采样最大约 0.0653。保留 ring 只保证“没有清空”，不保证读头变化连续。

**实施步骤：**

1. 给 registry 数值参数声明单位、平滑域和时间：gain 在 dB／线性增益域的选择固定；频率可用 log 域，time 需要独立读头策略。
2. Delay 选择成熟双读头交叉淡化，或明确可产生 Doppler 的连续变速；默认剪辑混音可优先短窗双读头，旧／新读头共享历史。
3. 淡化进行中再次改目标时接续当前状态，不反复重置／跳回旧头；左右 Link 原子更新。反馈路径和干湿路径使用明确稳定的过渡矩阵。
4. Reverb 几何读头／tone／RT60 分类更新；拓扑变化若确需双网络，限制并发次数和内存，旧尾不骤清。
5. 事件使用绝对 sample offset；重放自动化时效果不依赖 UI 刷新率和渲染块大小。

**验证：** 保存当前 step 数值作为失败基线；纯音、噪声、脉冲、反相 stereo，10→10.25、121→300ms 和高速反复拖动均无清尾／非有限值；用连续性误差、短窗高频瞬态能量及听音共同验收。不能对任意信号设统一相邻采样阈值；应与选定过渡算法的独立 oracle 和未变参参考比较。整块／随机块误差≤约定容差。

### R08：实时热点与性能发布门槛

**证据：** 本轮保存的测量见 [delay-measurements.json](../audit/2026-11-15-audio-mixer-review/delay-measurements.json)：48kHz／2000样本块预算 41.67ms，Delay=0／0.01ms 的单实例 median 约 144.8／80.3ms；1ms 约 5.37ms，121ms 约 0.53ms。计时随机器负载变化，但近零热点明确；反馈最短分段为 1 样本，每段触发 Python／数组／滤波调用。当前主窗的 p99 也明显高于 median，尚不能判为有足够余量。

**实施步骤：**

1. 在空闲机器固定 CPU／依赖版本测近零、最大反馈、交叉反馈、两声道、44.1／48／96k；区分冷启动、参数更新、恢复播放与稳态。
2. 先明确 0ms 的因果定义／最小一采样反馈。不要为了跑分快把所有小延迟强制改成 1ms，避免改变梳状响应和旧工程声音。
3. 将短反馈递推放到项目可维护的编译内核／原生块处理路径，或用有证明的递推向量化；减少每采样 Python 对象／scipy 调用。验证依赖许可、打包和平台回退。
4. 预分配 scratch，按需 FFT；表计约 30Hz 合并，UI 无消费者时关闭昂贵分析；处理链状态保持在音频线程受控所有权中。
5. 建立 1／8／16 轨、每轨五 FX、Bus／Master 的 block p50／p95／p99、sink underrun、端到端延迟和 peak RSS 报告。

**验证：** 波形对旧实现静态 oracle≤1e-5；边界 finite、反馈稳定；48k／256 samples=5.33ms 的工程目标按真实可用后端设定，建议 DSP p99 占预算≤50%，给解码和调度留余量。当前后端若实际使用较大块，同时记录块长和端到端延迟，不能只增大块长隐藏热点。计时不作为共享 CI 的脆弱毫秒断言，保留基准报告和变更趋势。

### R09：Dynamics Make Up 与两种形态

**证据：** 复合 Dynamics 已把 Make Up 放在全部动态处理后，F07 补仅 Make Up 的链激活；三实例 panel=dynamics 仍把 makeup 存在 Compressor，位于 Limiter 前。相同面板参数两种模型可产生不同限幅声音。

**实施步骤：** 列出 legacy／composite／three-instance 的链顺序与混合公式；用显式 DynamicsGroup 或 graph 独立 post gain 节点统一语义；保留每子模块旁路、总电源和 Mix；R01 负责旧形态兼容／显式升级；UI 和 transfer 图由同一参数契约生成，避免三个存储实例各自解释总增益。

**验证：** Comp 旁路、Limiter 开启、Make Up +12dB、Mix 0／50／100 的双形态对照；默认新模型输出一致；旧模型不经升级声音保持；整体旁路关开保留模块组合；preset／snapshot／undo／SC Listen 不能把 group gain 加两次。

### R10：设备、采样率与录音／监听生命周期

**确认边界：** 播放线程设备 fallback 更新线程／clock／mixer rate，但未完整经 manager 的 sample_rate_changed 路径通知上层，F02 只修收到通知后的窗口。原录音审计指出 R 是直接 start／stop，不是独立 arm；监听走独立 QAudioSink；ADR patch 不完整持久化；应用退出全局录音收尾仍需核验。

**实施步骤：**

1. 把协商后的 actual rate／channels／device generation 回报 manager，统一重建 decoder resampler／clock／DSP／ring 后通知 UI；与用户请求 rate 区分，避免递归重开设备。
2. 设备替换以 generation 原子切换，旧回调不得污染新时钟／遥测。工程 gain／routing 保留，运行时 device id 不冒充工程内容。
3. 分离 arm、record、monitor 三种状态；arm 是否持久化按产品决定；走现有 RecordingManager 状态机，不从 R 按钮绕开。
4. 监听若接 Mixer，明确 pre/post、mute／solo、延迟和反馈策略；若保留独立 sink，UI 准确表达其行为。输入 patch／延迟校准用可重建设置保存。
5. 应用关闭、设备拔出、磁盘满／写失败／取消录音都进入统一 finalize／partial-take 流程，等待写盘完成后退出；错误保留可恢复 take 和原因。

**验证：** 可注入设备桩先覆盖 negotiated 44.1k 而 requested 48k，所有曲线／频谱／解码时基一致；真实麦克风短 take、拔出、切设备、录音中退出、磁盘故障逐条记录；重启导入 partial take；输出不出现意外监听反馈。真机未测时保持“待硬件”，不能改文档为完成。

### R11：可信遥测与故障诊断

**范围：** 当前实例 tap 和轨级 spectrum 不是同一分析点；旁路 tap 把 in/out 都置地板，属于“旁路归零”产品选择，不能同时标为测得的直通峰值；动态检源内部／外部来源也应可解释。广泛 except／内部检源回退会让“表在动”掩盖能力缺失。

**实施步骤：** 定义版本化 Telemetry DTO（instance_id、stage、generation、sample cursor、units、valid／bypassed／silent）；明确 in/out、pre/post-fader、post-limiter、sample peak／RMS／momentary LUFS 的位置与窗口；旁路展示真实直通或灰色无效读数二选一并统一文案；SC source 不可用、循环、unsupported version 向诊断面板和导出 preflight 汇总；错误日志限频但保留原因，不把异常作为无限静默旁路。

**验证：** 两同类实例 gain 不同，输入／输出 tap 读各自值；旁路读数符合所选语义；Master pre/post、Bus、自然空段、seek 代数均正确；反相 stereo 频谱不抵消；关窗 FFT 计算减少且 PCM 不变；故意缺 SC／后端／版本时同时观察诊断与声音策略；新增测试用解析值／显式 PCM oracle，避免镜像生产实现或条件不满足时跳过断言。

### R12：多声道 Panner、LFE 与能力入口

**证据／限制：** vbap.py／lfe_route 的数学测试已经有价值，但普通轨 DSP 主域仍 stereo；Panner 5.1／7.1 舞台不能据此证明真实各声道输出。原 evidence 已承认这条边界。

**实施步骤：** 建立图上每边 layout／channel labels 契约；在 input decode→clip→track FX→panner→Bus→Master→export 全段保持格式；mono 点源、stereo spread、真正 multichannel bed 分开定义；VBAP 有效对／三元组预计算，LFE 单独低通／路由不参加全频能量归一；delay／reverb 的多声道处理选择明确，不交替复制 L/R 冒充空间输出；现阶段 UI 根据 capability 禁用未贯通控制或清楚标注 stereo preview，避免误认为已输出环绕。

**验证：** 2.0／5.1／7.1／高度布局逐源声道 impulse 扫描，文件 channel labels 正确；扬声器方向主增益对应目标通道；Only 模式能量只在 LFE；downmix 不默认回混 LFE；播放／导出／Stem 的布局一致；高度不支持时明确拒绝。没有多通道设备时可以先验离线逐通道，但硬件监听另列待办。

### R13：可选增强与最终交付门槛

Group／Link／VCA、True Peak／过采样保护、FX 的 Write／Touch／Latch、离散事件自动化、更多 pre/post-FX taps 可独立排期。它们有各自状态／时序／延迟成本，不能只添加菜单。True Peak 需公开滤波参考、dBTP 单位和延迟报告；VCA 不应改写成员已录制的音量曲线；编组手势一次提交、undo 原子。

每批完成须交付：失败基线、最终行为／兼容决策、独立数值 oracle、命令／存盘／重开测试、预览／成片／Stem 一致性、失败与取消证据。最终发布至少满足：

- R01–R05 中影响声音和数据的 P0 已解决，剩余 P1 限制在 UI／交付能力中明确表达。
- 当前 [remaining-issues.json](../audit/2026-11-15-audio-mixer-review/remaining-issues.json) 的失败案例转为能在原基线失败的回归测试。
- 用户视频完整主窗口回归继续通过；再加入检源轨、嵌套 Bus、重叠 clip、间隔、两同类 Delay 和 Master FX，而非只播放 A1。
- 本轮 F01–F14 无回归；没有用删除测试、放宽容差、静默降级或重命名“已完成”来代替功能补齐。

## 6. 两个新增效果器：原型审阅与统一接入任务

### 6.1 输入与可复用边界

已只读审阅以下用户 UI，未运行其 QApplication、未改动原型文件。文件中的注释作为设计资料分析，不作为执行指令。

| 原型 | 真实结构／已具备 | 接入时必须补齐 |
|---|---|---|
| [VoiceChanger-UI.py](</Users/heng/Documents/vscode/Topos test/VoiceChanger-UI.py>)，2453 行 | `StudioVoiceChangerWidget`，Formant／Pitch／Vocoder 三卡片、XY 声线画布、A/B、预设、Audition；26 个声音状态叶子字段 | 42ms timer 是合成语音频谱／电平；无变调或共振峰 DSP；缺 preview／commit 边界、实例上下文和持久用户库 |
| [SpectralMangler-UI.py](</Users/heng/Documents/vscode/Topos test/SpectralMangler-UI.py>)，2635 行 | `StudioSpectralManglerWidget`，Ring／Grain／Dist、四种路由、A/B／Random、Freeze、Mix／Gain；31 个声音状态叶子字段 | 40ms timer 产生模拟信号；LIM 未入状态；真实 wave／grains 接口只存数据不画；无颗粒、环调或非线性音频引擎 |

原型 SHA256、原始 demo／neutral 状态和旋钮范围已冻结到 [ui-contract-inventory.json](../audit/2026-11-15-new-audio-fx/ui-contract-inventory.json)。范围与默认值以该快照和下表为起点；UI 中的 `default_*_demo()` 是试听预设，不应在新建 FX 时自动改变用户素材。

建议采用两个**独立复合 FX 实例**：

- `voice_changer`：内部 Formant → Pitch → Vocoder；一个 `instance_id`、一个预设、一个原子命令。Pitch 可与 Formant 共享分析缓存，但必须保持下述串联和各自 Mix 的声音语义。
- `spectral_mangler`：内部 Ring／Grain／Dist 和显式 routing；一个 `instance_id`。不要拆成三个普通 Mixer 实例，否则随机状态、Freeze、内部路由和整组预设容易失去一致性。
- 均为 CPU 音频 FX，放在既有 FX 列表／Audio Effects 分类，可多实例、调序、复制、旁路；Mixer 不新增两个固定大胶囊或大卡片。窗口保留用户原创风格。
- 首版声明 mono／stereo；多声道仅在 R12 的完整图能力成立后开放，不通过复制 L/R 冒充多通道处理。

### 6.2 已确认的原型接线缺口

1. **演示动画与真音频分开。** 两原型构造就启动 demo timer。生产构造改 `demo=False`，初始地板／空谱／无效 F0；只在独立演示入口允许假数据。Qt 重绘 timer 可以保留约 25–30Hz，但不能生成音频读数、推进载波／grain 随机或决定音频时间。
2. **Spectral 真数据没有被画出来。** `_external_wave/_external_grains`（870–903）只初始化和赋值，`paintEvent` 仍使用内部 `_particles` 与模拟 `base_sig/apply_grain`（1165–1230）。需新增真实数据绘制分支；纯参数 waveshaper 图可以继续计算，但标明为响应示意。
3. **LIM 不生效。** `ManglerMasterStrip.limiterToggled`（649／711）已存在，主面板只连接 mix／gain（2323–2324），31 字段状态不含 limiter。增加 `output_limiter_enabled`；接入回填、预设、命令、DSP 和测试。CLIP 灯属于遥测，不入工程。
4. **手势没有提交边界。** 旋钮、XY／节点、推子只连续发 change；release 没有 commit。不能同时把模块 changed 和总 stateChanged 都连到工程命令。一个宿主适配器接管 begin／preview／commit／cancel；滚轮短时合并，双击、菜单、Freeze、A/B 切换、Reset、Random 各一次命令。
5. **外部回填不应创造声音状态。** `set_state` 直接覆盖整树，缺字段时多处索引会报错；应先经 Params 验证，再在锁内回填。删除／换工程／undo／snapshot 后处理实例身份与 revision，复用 R06，不让旧窗复活效果。
6. **Voice 的 CLR 不是可懂度测量。** `_tick_demo_analysis`（2325 起）按参数人为计算约 45–99%；公开 API 默认 90%。生产不可沿用这个数字。首版改为有明确定义的分析置信度／voicing，或显示 N/A；真正语言可懂度评价需单独有参考音频和算法依据。
7. **Audition 只有信号。** Voice 2044–2068 的按钮仅发 `auditionToggled`，没有内置音源和实际播放。首版改为“试听当前片段／选区”，走现有播放服务；不另建 QAudioSink。需要内置人声循环时另加有分发权的项目资源及显式选源，不从用户未知文件自动抓取。
8. **A/B／用户预设仍是内存状态。** `get_state()` 只返回活动 bank；Copy 复制到非活动 bank 不改变声音。按 R06 定义 session A/B，活动参数切换进命令；用户预设复用 `effect_preset.py`，内置只读，同名保存／删除不覆盖内置模板。
9. **小屏布局。** Voice 最小 900×620，Mangler 最小 920×620。宿主按屏幕 availableGeometry 限定初始尺寸；卡片区可折叠／滚动，图可缩放，125%／150% 缩放不裁操作。Mixer 一行短名＋完整 tooltip，继续保留紧凑轨宽。

### 6.3 注册、状态与宿主改动清单

以下现有文件已核实；“拟新增”路径是实施建议，当前并不存在对应实现。

| 位置 | 必要改动／复用入口 |
|---|---|
| `src/features/edit/core/timeline_v2.py` | 新 `VoiceChangerParams`／`SpectralManglerParams`；`_ensure_effect_param_registry` 注册两个 type；每效果 schema／algorithm version；数值 finite、范围、enum 与 unknown payload 处理 |
| `src/features/edit/core/effect_registry.py` | 新两项 `EffectDefinition`，完整 `ParamSchema`，`_get_audio_default_params`、`AUDIO_MIXER_EFFECT_TYPES`、`EffectRegistry._register_builtin_effects`；新建 clip／Mixer 同一默认值 |
| `src/features/edit/core/effect_instance.py::ParamSchema` | 使用已有 float／int／bool／choice；所有负值、单位、step、枚举完整声明，不能走默认 min=0 的 fallback |
| `src/features/edit/core/audio_dsp.py` | `_ensure_effect_registry` 加工厂；`AudioEffect/EffectChain` 接时间／延迟／尾音上下文。复杂 DSP 不继续全部堆入这个大文件 |
| 拟新增 `src/features/edit/core/audio_effects/` | `voice_changer.py`、`spectral_mangler.py`，以及公共 streaming／analysis primitives。若需要共享 `AudioEffect` 基类，先抽 `base.py` 并由旧模块兼容重导出；`_clamp_finite` 等共同 helper 也从无环基础模块导入，禁止子模块反向导入正在初始化的 audio_dsp 造成循环 |
| 拟新增 `src/shared/ui/audio_mixer/voice_changer_dialog.py`／`spectral_mangler_dialog.py` | QDialog 包装用户 QWidget；构造兼容 `(track_id, track_name, instance_id, initial_params, parent)`；新增 Params↔UI state 适配、更新锁、preview／commit、真实遥测、reset |
| `src/shared/ui/audio_mixer/track_mixer_widget.py` | `_effect_default_params/_effect_display_name`、菜单、双击／Enter 分发、短名／tooltip、两类窗口字典加入 `_EFFECT_DIALOG_ATTRS`、`_show_effect_dialog`、`_sync_dialog_toggle`、`update_track_telemetry`；保留对象身份清理与删除守卫 |
| `src/app/controllers/edit_controller.py` | 复用 `_on_mixer_effects_preview/_committed` 和轨／Master 命令；不要每个 valueChanged 直接提交。`_collect_fx_param_specs` 除读完整 schema，还须使用自动化白名单／automation_kind，排除版本／seed／资源引用和离散 band-count；当前仅按 int／float 枚举并不够 |
| `src/features/edit/ui/clip_audio_effect_dialog_adapter.py` | `_ensure_dialog_registry` 注册两类窗口；当前 manager 仅接 committed，承诺 clip 实时试听前必须补 preview 分发和 cancel 恢复 |
| `src/features/edit/core/audio_retime.py` | `clip_fx_entries` 由三元组升级到含 `EffectInstance.effect_id` 的四元组；`animated_fx_sampler` 同步保留身份；三路径调用方、type hints、旧解包和测试一起迁移，修复同类 clip FX 调序串历史与空 tap id |
| `src/features/edit/core/audio_playback.py`／`audio_graph_renderer.py` | 普通轨／Bus／Master 同 registry DSP、绝对 sample cursor、PDC、预热／drain／reset；Bus／Master 自动化仍依赖 R05 补齐，不能只注册类即宣称全域支持 |
| `src/features/edit/core/timeline_export.py`／`stem_export.py` | R02 单调调度、R03 共图 Stem、每实例后端 preflight、Freeze 资源和预卷、限域尾音策略；归一化仍只重复保护限幅 |
| `src/features/edit/core/effect_preset.py`／`mixer_preset.py`／`commands/audio_commands.py` | 两类完整声音参数与 seed 进入 preset／快照／undo／保存重开；复用 SetTrackEffectChain／SetMasterEffectChain，版本和 unknown 按 R01／R05 处理 |
| `src/features/edit/core/audio_capabilities.py`／`audio_health.py` | 逐处理器依赖、布局、质量档、时延与缺后端原因；导出 preflight 不再仅检查 EQ／LUFS 所需 scipy |

音频生产注册入口是 Edit 的 EffectRegistry；shared EffectCatalog 的视频／GLSL spec 分支不应增加假的音频 shader。必要的能力汇总仍要出现在同一应用健康／交付诊断中。

### 6.4 生产参数采用扁平 schema，UI 保留嵌套结构

当前 `TrackEffectInstance`、chain entries 只复制顶层 dict；`build_track_fx_sampler` 仅覆盖顶层 `pkey in params`，`ParamSchema` 没有 nested object。**推荐扁平持久参数＋双向 UI adapter**，而非把原型 nested dict 原样塞进工程。

- 例：UI `state['formant']['shift']` ↔ 模型 `formant_shift_st`，UI `state['grain']['density']` ↔ 模型 `grain_density_hz`；从头到尾只维护这一份映射表，禁止 UI／自动化／DSP 分别猜字段。
- 自动化键沿用 `fx/{instance_id}/formant_shift_st` 等顶层 key；旧链无新效果不迁移。原型 demo presets 在导入时一次转换，读回往返未知字段由 R01 统一保存。
- bool／enum／band-count／routing／Freeze 是离散事件，不能用 float 插值伪造；连续数值在 DSP 的 sample clock 上平滑。现阶段没有事件 lane 时，先支持静态命令并在 UI 明示未可录制。
- `schema_version`、`algorithm_version`、`random_seed` 为持久元数据，不向普通连续 lane 暴露。seed 用明确整数范围（如 uint32）；复制实例默认继承 seed 保持声音，Random 可生成新 seed 并随参数一次提交。
- 在 schema／definition 增加或关联 `automation_kind=continuous/event/never`（拟新增契约）；当前 `_collect_fx_param_specs` 会枚举顶层数值，必须实际改为白名单，不能靠“不显示”文案阻止 seed／版本／freeze anchor 进入数值曲线。Vocoder bands 虽是 int，仍属 event。
- 未知字段无损、空值清除、A/B、Master 快照都继承 R01／R05／R06，不能为新效果再做一套只写版本标签的存储。

### 6.5 不能跳过的共同前置：时延、实例身份与真实时间

现有 `AudioEffect` 没有通用 `latency_samples`，DSP `process(samples)` 不收绝对时间。`StreamingWsola.algorithmic_latency_samples` 只是 decoder 内的保调变速预读，**不表示 Mixer 图已有 FX 延迟补偿**。

1. 新建 `ProcessContext`／等价节点上下文：sample_rate、absolute_start_sample、frame/time mapping、generation、discontinuity、block_count；每实例状态由音频节点拥有，UI 只发控制变更。载波／grain／jitter 不读 wall time。
2. 每节点报告固定质量档下的处理 latency／预读需求／tail；节点 wrapper 负责旁路延迟。延迟不是尾音：纯 algorithmic FIFO 也必须在末尾 flush。
3. 每个模块内部 Mix、Mangler 并联、全局 dry／wet、Send／Bus 汇合对齐；质量／routing／模块旁路改变时不能让音频相对视频跳动。优先为一个实例固定最大处理时延，再对短路径补齐。
4. 离线从非零选区起点渲染时，预卷／重建状态后裁出目标区间；不只减一个整数延迟就认为状态重建完成。实时 seek／循环需要同样定义的重放／checkpoint 策略。
5. 普通轨已有 instance_id；clip 需补 effect_id 四元组。新建／复制／调序／删除要明确种子和历史是否继承；不能按 effect_type 把两个 grain 历史交换。
6. 随机由 seed＋绝对事件序号／sample position 驱动，不能每次 process 调一次 random；否则改变块长、打开窗口或多一次 UI 回填都会改变成片。

首阶段可开发纯 DSP／UI adapter，但只有 R02／R03／R04 对应能力贯通后，才宣称实时、成片、Stem 和任意选区“无缝一致”。

### 6.6 NF-04：新增真实遥测的生产与消费接口

现有 `EffectChain.process` 只构造固定 in/out／GR／SC／overshoot，不会自动产出 F0、module activity 或 grain 元数据。必须把生产端和窗口端一起接通：

1. 新 processor 提供可选 `collect_telemetry()`／typed analysis snapshot；包含 instance id、generation、absolute sample、valid／silent、输入／输出电平、分析谱、F0／voiced／confidence、模块能量指标。Mangler 增 waveform min/max、有限数量 active-grain 描述、Freeze pending／event id、clip／limiter 指示。
2. 普通轨／Bus／Master：processor → EffectChain 实例 tap 扩展 → `TrackLevel.taps` → `AudioPlaybackThread._write_next_output_chunk` → manager／Panel → 该 instance 的 Dialog。为所有新增字段建立 DTO／单位／限长，不直接跨线程传递可变 DSP 数组或整个 history ring。
3. **clip 单独补齐身份通道。** 当前轨 tap 只取轨／Bus／Master chain，ClipDialogManager 没有遥测接线。新增按 `(clip_id,effect_id)` 聚合的 clip-instance tap，并由 clip dialog manager 分发；重叠 clip 不能共用一个轨末端谱冒充各实例输入／输出。
4. 昂贵 FFT／F0 按窗口订阅与分析预算执行，能复用 DSP 已有分析缓存则复用；纯分析可在受控 worker，音频 callback 不等待 UI。UI 约 30Hz 合并最新 snapshot，波形先作保峰值抽稀，颗粒数量／载荷有上限。
5. Voice 使用 `update_analysis/update_meters` 的生产适配，明确无 F0／不可靠 confidence 的 N/A 绘制；不向 QPainter 传 NaN，也不默认 CLR=90。Mangler 修改实际 paint 分支消费新 wave／grains，保留纯参数响应示意的独立来源。
6. 新增 `reset_telemetry(generation)`，stop／pause／seek／自然空段清空谱、wave／grains、F0／模块活动与 clip 灯；丢弃旧代 queued 数据。当前 `setTelemetry(None,None)` 只忽略输入，不能当作清理。

验证时使用不同频率／gain 的两个同类实例与两个重叠 clip；分别观测独立数据、停止清理和关窗取消订阅，同时确认关闭窗口不会改变 PCM。

## 7. Voice Changer：参数契约与后端实施

### 7.1 原型参数到生产 schema

下表默认取原型 `default_voice_neutral()`，保留 UI 的零点和单位；与 `default_voice_demo()` 不同。新增实例不自动加载女声／机器人等 demo。模型键为建议 v1 稳定键，实施时一次确定并版本化。

| 原型路径 → 模型键 | 类型／范围 | neutral 默认 | DSP／交互语义 |
|---|---|---|---|
| enabled → enabled | bool | True | 总旁路保留三个模块组合；有效开关同时受实例 enabled 控制 |
| output_gain → output_gain_db | dB −20..20，step .1 | 0 | 全部模块后一次增益 |
| formant.enabled → formant_enabled | bool | True | 中性状态须是延迟对齐后的恒等 |
| formant.mode → formant_mode | Natural／Crisp，稳定小写 id | Natural | 分析／瞬态处理策略，不能只是颜色差异 |
| formant.shift → formant_shift_st | st −12..12，step .1 | 0 | 改谱包络，保 F0；比例 `2**(st/12)` |
| formant.throat → formant_throat_pct | % −100..100 | 0 | 低／中频包络频率轴的平滑非线性 warp |
| formant.gender → formant_gender_pct | % −100..100 | 0 | 不同包络频段差异 warp，保持单调；是一种音色宏，不是身份／性别检测 |
| formant.preserve → formant_preserve_pct | % 0..100 | 90 | 清辅音／瞬态保护量，不等于整体干湿 |
| formant.timbre → formant_timbre_pct | % −50..50 | 0 | 有界 tilt EQ；明确 %→dB/oct 映射 |
| formant.mix → formant_mix_pct | % 0..100 | 100 | 本模块线性幅度 dry／wet，延迟对齐 |
| pitch.enabled → pitch_enabled | bool | False | Pitch 总模块开关 |
| pitch.tracking → pitch_tracking | Vocal／Wide | Vocal | 有声分析范围／瞬态策略；Wide 不能只把单声道人声假设扩大标签 |
| pitch.semitones → pitch_semitones | st −24..24，step .1 | 0 | 保持片段时长的变调 |
| pitch.fine → pitch_fine_cents | cents −100..100，step 1 | 0 | 与 semitones 合成同一比例，不重复移调 |
| pitch.formant_link → pitch_formant_link_pct | % 0..100 | 0 | 0 保当前谱包络；100 随本模块 pitch 比例一起移动；不重复应用独立 Formant shift |
| pitch.glide → pitch_glide_ms | ms 0..200 | 18 | 平滑目标 pitch 比例；不是改变 clip speed 或拉长片段 |
| pitch.jitter → pitch_jitter_pct | % 0..100 | 0 | 带限、零均值微小 pitch 调制；%→最大 cents／相关时间需校准并锁版本 |
| pitch.mix → pitch_mix_pct | % 0..100 | 100 | 0 对应本模块完全 dry，link／jitter 也不污染 dry |
| vocoder.enabled → vocoder_enabled | bool | False | Modulator 为前两模块输出，首版载波为内部合成 |
| vocoder.carrier → vocoder_carrier | Saw／Pulse／Choir／Noise | Saw | 四个真实、有区分的载波生成器 |
| vocoder.bands → vocoder_bands | int 8..32 | 16 | 离散改变滤波组；切换需有界重建／交叉淡化 |
| vocoder.carrier_hz → vocoder_carrier_hz | Hz 50..440 | 115 | 合成载波基频；Noise 无固定 F0 时显示 N/A，参数可保留但禁用调节 |
| vocoder.formant → vocoder_formant_st | st −12..12 | 0 | Vocoder 合成频带／包络轴偏移，独立于 carrier pitch |
| vocoder.unvoiced → vocoder_unvoiced_pct | % 0..100 | 72 | 与检测到的清音残差混合，时间对齐 |
| vocoder.release → vocoder_release_ms | ms 5..500 | 24 | 包络释放时间；attack 可先固定但须在算法契约记录 |
| vocoder.mix → vocoder_mix_pct | % 0..100 | 100 | 对本模块输入的干湿，不额外叠一份轨道原始 dry |

另增版本、质量档和 seed 元数据；`audition`、活动 bank、窗口尺寸、analysis confidence 和 meter 数值不混入以上声音参数。原型 Formant／Pitch／Vocoder 的 GUI reset 值与 demo 值不同，Reset／Init／加载预设应各自明确。

### 7.2 VC-01：保持时长的 Pitch 与流式封装

**成熟路线：** 初版技术验证优先比较项目现有 WSOLA＋带限重采样与相位声码器（相位连续／锁相＋瞬态处理）；人声高质量／共振峰独立控制优先 source-filter／频域方案。现有 `audio_timestretch.py` 做保调变速，不能直接把 `clip.SpeedParams` 改成变声参数。

**实施步骤：**

1. 独立 `PitchProcessor`，比例 `r = 2**(semitones/12 + fine_cents/1200)`，正常时间比例始终 1；处理节点输出与请求 block 相同采样数，内部 FIFO／预读／结束 flush 有上限。
2. 流式 WSOLA＋resample 验证时，先 time-stretch 到约 `r*N`，再带限重采样回 N；不能反过来把伸缩方向写错，也不能每块重新开 WSOLA／重采样器。
3. 若采用 phase vocoder，持有 STFT analysis／synthesis phase、窗与 OLA norm、瞬态处理；pitch 路径对包络的改变由 Formant Link 校正，不靠播放速度变化。
4. Vocal／Wide 先测分析窗、F0／瞬态和延迟的实际取舍再定质量档。输出立体声共用时间映射，分析参考用声道能量／相关性，避免 `(L+R)/2` 把反相声音抵消。
5. Glide 在 sample clock 上平滑 log pitch；Jitter 为有界、种子可复现的低频过程，不是每块白噪声；静音／低置信度输入不强行追踪虚假 F0。
6. 封装可替换 backend，后端选择／版本写入能力报告。可选 Rubber Band 仅在依赖、许可、原生打包与固定输出适配通过后引入；现有仓库没有声明该依赖。

**验证：** 220／440Hz 输入在 ±12st 为 110／220／440／880Hz 的正确对应，稳态误差目标≤5 cents；完整 −24..24st／fine 端点有限；对齐裁切后输出长度不变；随机块长、首尾短片段、非零起点、重放自动化一致。目标频率由输入和公式独立计算，不能只检查“参数变了”。

### 7.3 VC-02：独立共振峰、Throat／Gender 与辅音保护

**成熟路线：** 流式 STFT＋平滑谱包络（cepstral／true-envelope 或稳定 LPC）分离激励细节与声道包络，变换包络后重合成；采用 WOLA 完整归一。DAFx 的谱包络保留方法可作算法依据，原型画出的三组 Gaussian 峰仅为示意。

**实施步骤：**

1. 用 synthetic source-filter 语音 fixture 独立确定 F0 与 formants；为静音、清音、非人声、极低 F0 加 floor／置信度与增益上限。
2. 令 `E(f)` 为平滑包络，`R(f)=X(f)/max(E(f),eps)` 为细节；频率轴映射 `W(f)` 后 `Y(f)=R(f)*E(W_inverse(f))`，保持有意义的相位／瞬态；插值边界和 Nyquist 外衰减有定义。
3. Shift 控制整体包络比例；Throat／Gender 使用平滑、单调的分段轴映射，非对称调整不同包络区，禁止 F1／F2 交叉或负频率。原型的 0.0018／0.0026 等演示常数只能作为试听起点，不能未经测量当标准。
4. Preserve 根据清音／高频残差／瞬态检测，在处理声与时间对齐的保护支路间混合；不把 Preserve=90 解释为整个声音 90% dry。
5. Timbre 定义有限 tilt；Natural／Crisp 在包络分辨率、瞬态保留或窗策略上有可测区别。中性参数应走相同 latency 的恒等路径，避免重合成改变完全未调的声音。
6. Pitch 后端若已默认随 pitch 移动包络，应先补偿这一变化，再按 Link 的目标移动，防止独立 Shift、Pitch 默认 formant 漂移与 Link 三次叠加。

**验证：** Formant-only +6st：稳定 F0 变化目标≤5 cents，合成包络峰接近约定 warp（测量频段误差建议≤5%，Nyquist 边界单列）；pitch-only 且 Link=0 时原包络位置基本保持；Link=100 时按 pitch 比例变化。/s/、/sh/、/t/、呼吸、爆破音、不同音域、L=−R、双声道不串声；无声时不产生 DC／自激。真人听音评估“自然／清晰”单列，不用一个伪造的 CLR 百分比代替。

### 7.4 VC-03：8–32 带 Channel Vocoder

**实施步骤：**

1. 采用对数／感知分布的分析／合成滤波组，频率上界受当前 Nyquist 限制；分析输入经逐带包络 detector，attack／release 样本时间一致。
2. 同频带 carrier 乘相应语音包络再求和；Saw／Pulse 使用带限振荡器或有限谐波合成；Choir 定义为多振荡器轻失谐／音色组合，Noise 用 seed＋sample clock 的确定性噪声。
3. Bands／carrier 类型改变时只在受控边界建新滤波组，用有限交叉淡化切换；频带增益归一，不能 bands 从 8→32 自动放大 4 倍。
4. Vocoder Formant 改合成包络／滤波频带，Carrier Hz 改激励 F0；Unvoiced 加已检测的清音残差而非整条 dry 反复叠加。
5. 零 modulator 只允许已有 release 尾音继续，衰减后 carrier 不泄漏成持续音；内部振荡相位不因 UI 更新重置。

**验证：** 单带／扫频 modulator 对应正确合成带；release 的毫秒响应跨采样率一致；bands 整数边界有限；Noise 无 F0 时显示无效而非 carrier_hz；8／16／32 bands 的响度变化在声明范围；输入结束后 envelope 可预测衰减；有声／清音过渡保可懂度。

### 7.5 VC-04：组合链、试听与真实分析

1. 固定 Formant → Pitch → Vocoder → OutputGain；三模块各自 `lerp(aligned_input, wet, mix/100)`；总旁路恢复本节点延迟对齐的输入且保留子开关组合。
2. 共享分析缓存按实例／sample cursor 管理，不能让一个模块重新推进另一个模块状态；禁止同一块处理两遍来“补”另一个 meter。
3. 输入／输出谱来自该实例两端；F0_in／F0_out 必须分别实际检测并带 voiced/confidence，不能输出 `f0_in*r` 当“测到的输出”。三模块 activity 使用真实处理能量／激活指标并定义单位；无可靠 CLR 指标则 N/A 或改名 CONF。
4. Audition 调用现有 transport 播放当前选区／片段，关闭窗口或试听结束恢复按钮状态；参数试听走 preview，取消恢复模型。正在播放时不启动第二套播放器。
5. 把原型内置 preset 按扁平 schema 校验一次，坏预设有诊断；默认 Neutral，用户明确选择后才应用声音。

**验收：** 多实例并开、三模块任意旁路组合、Mix=0／50／100、Formant Link 与 Shift 同时使用、A/B／Copy／Reset／预设／输出增益、普通轨／clip／Bus／Master 均可解释；Audition 不改变 clip 时长／播放速度／工程路由。

## 8. Spectral Mangler：参数契约与后端实施

### 8.1 原型参数到生产 schema

该原型的实际处理模块是环形调制＋时域颗粒＋waveshaping；首版不需要为了 “Spectral” 名称额外虚构 FFT 频谱重排。下表默认为原型 Init：关闭三个子模块、global_mix=100、gain=0，其余保存原 demo 数值作为再次开模块的起点。

| 原型路径 → 模型键 | 类型／范围 | Init 默认 | 必须确定的声音语义 |
|---|---|---|---|
| enabled → enabled | bool | True | 整体旁路，不改子模块配置 |
| routing → routing | 稳定 enum id | ring_grain_dist | 显示四种原型文案，保存 id，不保存翻译／Unicode 箭头 |
| global_mix → global_mix_pct | % 0..100 | 100 | 0=dry、100=拓扑输出；dry 延迟对齐 |
| output_gain → output_gain_db | dB −24..12，step .1 | 0 | global mix 后输出增益 |
| ring.enabled → ring_enabled | bool | False | Ring 独立旁路 |
| ring.mode → ring_mode | sine／dual／fm_ring／rectify | fm_ring | 四种明确不同的 carrier 算法 |
| ring.carrier → ring_carrier_hz | Hz 10..5000 | 340 | 音频时钟中的频率，非画布屏幕 cycles |
| ring.lfo_rate → ring_lfo_rate_hz | Hz .05..40 | 2.4 | LFO 连续相位 |
| ring.lfo_depth → ring_lfo_depth_pct | % 0..100 | 35 | 建议以有界 cents／频率倍率调制 carrier，锁定映射 |
| ring.warp → ring_warp_pct | % 0..100 | 28 | 模式相关波形／FM 指数，需逐模式表与极值保护 |
| ring.spread → ring_spread_pct | % 0..100 | 45 | 左右载波相位差；mono 不伪造 stereo 声道 |
| ring.mix → ring_mix_pct | % 0..100 | 65 | 本模块 dry／wet |
| grain.enabled → grain_enabled | bool | False | 颗粒模块独立旁路 |
| grain.freeze → grain_freeze | bool／事件 | False | 冻结内容与时刻，不能仅保存一个 True，见 SM-02 |
| grain.mode → grain_mode | cloud／reverse／scatter | cloud | 触发分布／方向的明确模式 |
| grain.size → grain_size_ms | ms 5..500 | 85 | 定义为输出粒长，确保 pitch 改变不误变 grain 持续时间 |
| grain.density → grain_density_hz | grains/s 1..80 | 24 | 单位显示 grains/s；由 sample clock 触发 |
| grain.pitch → grain_pitch_st | st −24..24 | 7 | 分数读头倍率 |
| grain.pitch_jitter → grain_pitch_jitter_pct | % 0..100 | 22 | 建议 100%=±12st 的有界每粒随机量，试听校准后固定版本 |
| grain.position → grain_position_pct | % 0..100 | 30 | 在合法历史读区间定位，建议 0=最新合法位置、100=最旧 |
| grain.spray → grain_spray_pct | % 0..100 | 45 | 合法区间的随机读偏移幅度；不读未来输入 |
| grain.feedback → grain_feedback_pct | % 0..95 | 38 | 指定归一后的 grain wet 回到输入 history 的反馈；不能反馈 global gain／LIM 后输出 |
| grain.mix → grain_mix_pct | % 0..100 | 70 | 本模块干湿 |
| dist.enabled → dist_enabled | bool | False | 失真独立旁路 |
| dist.curve → dist_curve | tape／fold／hard_clip／bitcrush | fold | 函数响应／动态滤波定义见 SM-03 |
| dist.drive → dist_drive_db | dB 0..36，step .1 | 14.5 | 幅度 `10**(drive_db/20)`，不照抄 UI 的 /24 |
| dist.fold → dist_fold_pct | % 0..100 | 42 | 按曲线映射折叠／饱和／量化强度，所有可操作参数需真实有效 |
| dist.bias → dist_bias_pct | % −100..100 | 12 | 非对称预偏置，后接去 DC |
| dist.tone → dist_tone_hz | Hz 200..16000 | 5200 | 声明滤波形状与实际频响，限于当前采样率合法范围 |
| dist.resonance → dist_resonance_pct | % 0..100 | 25 | 到稳定有界 Q 的映射，不允许自激无限放大 |
| dist.mix → dist_mix_pct | % 0..100 | 80 | 本模块干湿 |

新增 `output_limiter_enabled`（建议生产 Init=False，Demo 可开启）、版本、quality、seed 和 Freeze 资源／时间引用。把原型私有 `_limiter=True` 改成从 Params 回填；总旁路不运行这个 FX 的保护限幅，Master 的既有保护仍独立存在。

### 8.2 SM-01：Ring Modulator、相位与立体声

**实施步骤：**

1. 以连续相位累加器生成 carrier，`phase += 2*pi*f/fs`；block 划分、开窗、preset 回填不重置相位。频率／depth／warp 用 sample ramp，seek 由统一上下文重建。
2. Sine 标准 `wet=x*sin(phase)`；Dual 定义双载波的固定频率关系／权重；FM-Ring 定义频偏或相位调制指数；Rectify 明确为整流 carrier 再乘输入，不能不说明就变成整流素材。所有模式从一个稳定的映射版本表生成。
3. Spread 控制 L/R carrier 相位偏移；参数不通过互相污染的 in-place 写入计算。对本已双声道素材保留输入差异，mono／stereo 行为分别测试。
4. 对非正弦 carrier／FM 高频边带评估带限／过采样，Rectify 的 DC／偶次特征按设计处理，不能把所有边带误当 bug 或所有 alias 当“效果特色”。

**验证：** 输入 1000Hz、Sine carrier 200Hz 出现 800／1200Hz 边带；同幅度理论端点与 FFT oracle 对照；mix=0 恒等，carrier 参数可测；0 spread 左右一致，非零 spread 有定义差异；整块／随机分块相位一致。

### 8.3 SM-02：真实 Granular、Freeze 与确定性

**实施步骤：**

1. 预分配多声道 history ring、grain voice pool 和 window 表；每 grain 保存 start sample、source position、read ratio／direction、output duration、phase／window、gain。Cloud 为有定义常规调度；Reverse 读反向历史；Scatter 用 seed 驱动触发／位置扰动。
2. grain pitch 读速 `r=2**((pitch_st+jitter_st)/12)`，读头做带限／分数插值；Hann 等窗合成。density 按绝对采样时间的相位累积，不能每块 `int(density*block_seconds)` 后丢小数。
3. 以输出粒长定义时，steady max overlap 约 `ceil(80*.5)=40`，voice pool 可先给 64 个有界槽；参数快速变化、切换时单独测上限。无空槽时有明确策略／计数，不在音频回调动态分配无界列表。
4. Ring 容量按整个 grain 生命周期相对 writer 的最大历史年龄算：最大总 pitch=24+12=36st，r_max=8，500ms 粒的 source span 是 4s；Reverse 同时还经历 writer 前进 0.5s，因此至少需约 **4.5s＋block／插值 guard**，再加允许的 position／spray 回看余量。设初始回看 D、输出经过 t，Reverse 的历史年龄为 `D+(r+1)*t`；必须全生命周期满足合法边界，不能只在触发时检查。position／spray 的可选区域随容量、有效历史、粒长和读速收缩；新启动不足历史按零填／延后处理，不取模读未来或旧垃圾。
5. 合成按有界 window 权重／能量规则归一后再反馈，feedback=0 时无回写；非零反馈有 DC blocker／有界保护，回路增益与 grain 数无关。Freeze 停止源／反馈对冻结 history 的写入，feedback 值保留给解除 Freeze 后使用。
6. 每 grain 随机量由 persisted seed、grain ordinal 和独立 stream id 生成；视觉粒子消费 DSP 的 grain 描述，不再自己随机生成“真实颗粒”。改变窗口刷新率不得改变声音。

**Freeze 生产方案：**

- 单个 `grain_freeze=True` 不能重建当时 history。推荐 Freeze 事件在明确的 absolute sample 边界获取模块输入 history，生成项目管理的 lossless capture asset，并保存 `freeze_capture_asset_id`、`freeze_anchor_sample`、格式／算法版本及重放所需调度元数据；JSON 只存引用和小型 metadata，不塞 PCM 缓冲或绝对机器路径。
- 捕获到保存采用可取消事务：音频线程在 sample cursor 确认 anchor，以预分配缓冲交换／有界快照交后台写盘，不由 UI 读取运行中 history，不在音频 callback 分配大数组或做文件 IO。pending 期间即时使用同一 anchor 的临时冻结状态，不能把写盘完成时间当冻结时刻；成功一次 command 提交引用，失败淡化回滚并报告。Undo／删除只解除使用关系，资源回收走项目既有引用规则，不能直接删其他实例仍用的素材。
- 多次 Freeze 每次具有稳定 `event_id → capture asset／anchor／metadata` 记录；实例的当前 asset_id 不能覆盖此前事件的来源。事件历史、undo／redo、选区重放使用同一记录，解除 Freeze 也带明确 sample 时刻。
- v1 可以规定 Freeze 边界将旧 grains 短淡出、新冻结 grains 从 anchor 和固定 seed 重新调度，减少不可重建的隐式运行状态；该过渡必须写进算法版本并听测。若要完全连续继承所有活跃 grains，capture metadata 必须同时包含它们的读头／包络位置和 PRNG counter。
- 任意区间导出从 capture／anchor 恢复并预卷所需节点；资源缺失是 preflight 错误。解除 Freeze 按明确淡化转回实时 history；空 history 时不创建伪快照。只做“从源头完整重放”也可以用于开发验证，但不能宣称已解决任意起点／改源之后的冻结内容持久化。
- Freeze 可无限持音，节点应报告无限／需显式结束的 tail 类型。stop／seek／严格范围导出按 R04 策略处理，不把 infinite 冻结伪报为固定 4s tail。

**验证：** 整块／随机块、同 seed／同绝对区间的 grains 和 PCM 一致；随机 seek 后重建与从头渲染裁切一致；两实例调序不交换历史；不同 seed 改变结果；高／低 pitch＋最大粒长不越界；95% feedback 长跑有限；Freeze→保存重开→任意选区成片／Stem 复现同内容；捕获取消／写盘失败／缺 asset 均不留下错误工程引用。

### 8.4 SM-03：Distortion、Tone 与输出限幅

**实施步骤：**

1. 四模式共享一份纯响应／参数映射服务，UI curve 和 DSP 使用同源定义。Tape 可从归一 tanh 软饱和起步，Fold 用有定义的反折函数，Hard Clip 明确阈值，BitCrush 同时定义量化位数与 sample-hold；`fold` 在每模式的含义写明并显示适当标签。
2. Drive 是标准 dB 幅度增益。Bias 进入非线性之前，之后去 DC；Tone 用稳定滤波器，Resonance 的 Q 上限、频率边界明确，参数热改保留状态／平滑。
3. Tape／Fold／Hard Clip 在选定质量档采用有状态 2×／4× oversampling 或经过验证的 antialias 方法；插值／抽取滤波器历史跨块保留，群延迟进入 PDC。不能每块独立 `resample_poly` 后拼接，造成边缘瞬态。
4. BitCrush 的有意 sample-hold／量化噪声是声音语义，与非线性无意 alias 区分；anti-alias 质量档不能无声改变 preset 的 hold rate／bit-depth 映射。
5. LIM 位于 global mix／output gain 后，阈值和 release 若固定也写在算法契约；不替代 Master 安全限幅，也不冒充 True Peak。clip 灯、overshoot／GR 都来自处理结果。

**验证：** +6dB 对线性测试段为约 1.995 倍；对称输入 Bias=0 的 DC 接近 0，非零 Bias 去 DC 后不持续积累偏置；每曲线独立 oracle、Tone 扫频、Resonance 极值、feedback 联用有限；同一正弦下过采样质量档的 alias 能量有量化改善；LIM 关开、gain>0、输入结束 release、随机块均有回归。

### 8.5 SM-04：四路拓扑、Mix 与切换

原型 Parallel 模拟把三路除以 2.2，全部旁路时变成约 1.364 倍；不能移植到 DSP。建议 v1 使用下面可验证契约：

- 三种 serial 按 routing 顺序处理；每模块先得到 wet-only 结果，再以该模块 mix 与其时间对齐的输入插值。
- Parallel 对各启用模块从同一对齐输入产生 wet-only `F_i(x)`。令 `N` 为启用模块数，`m_i` 为各 mix／100，使用 `y=x + sum(m_i*(F_i(x)-x))/N`；N=0 返回 x。这避免重复三份 dry；所有 mix=0 和所有模块恒等时严格 unity。启用但 mix=0 的模块仍计入 N，保证拖 mix 到零不突然改变其他支路权重。
- enabled 改变会改变 N 和其他支路权重，切换时对旧／新合成权重作短交叉淡化，不只淡化被切换模块。
- 再以 global mix 混合 x／y，乘 output gain，最后可选 LIM。global mix=0 仍有声明的 output gain／LIM；整个实例旁路则保留时延后直通。两者不能混为同一个开关。
- 一个实例的固定延迟预算可取该质量档三模块串联最大处理延迟，短路／parallel 分支补齐到同一预算，grain 的“创作读位置延迟”与算法 latency 分开表示。
- routing 改变属于事件。首版可短 fade-out→切拓扑／必要重建→fade-in；更高质量可用有界双 graph 交叉淡化。不能用同一有状态对象对同一块先算旧顺序再算新顺序，造成 grain／phase 推进两遍。
- Random 一次生成完整经过 schema 校验的参数及新 seed，再一次提交；停止 Freeze、固定 −3dB 都不是安全保证，反馈和最终保护按实际声音工作。

**验证：** 路由三种顺序在已知非交换模块组合下有正确差异；Parallel 的恒等、全 bypass、全 mix0 和单模块端点精确；各支路 impulse 对齐≤1采样；routing／模块开关／质量切换不爆音、不换随机流、不让时间线漂移；关闭窗口后音频结果不变。

## 9. 实施顺序、任务表与端到端验收

### 9.1 新任务依赖

| ID | 任务／交付 | 前置 | 验证／完成门槛 |
|---|---|---|---|
| NF-01 | 26／31 字段映射、两 Params、完整 schema／registry／版本／能力声明 | R01，§6.4 | UI↔Params 往返；负数／enum／NaN／unknown／旧工程覆盖 |
| NF-02 | 两个 QDialog adapter、Mixer／clip UI 入口、手势与外部回填 | NF-01，R06 | preview 无命令；release 一命令；同类多窗／删除／重开正确 |
| NF-03 | PDC、绝对 sample context、clip id、预卷／flush／tail | R02／R03／R04 | dry／wet／Bus／并联对齐；非零选区与 seek 重现 |
| NF-04 | 逐实例真实 telemetry、无效／静音状态、图形消费 | R11，NF-02 | 两同类实例不串谱；pause／stop／空段清表，迟到代数丢弃 |
| NF-05 | preset／A-B／seed／Freeze 资源、快照、事件自动化 | R01／R05，NF-01 | 原子保存／undo／reopen；Random／Freeze 可重放；缺依赖有诊断 |
| NF-06 | 后端选择、安装包／许可证、性能与内存基准 | 原型 DSP | 目标平台／质量档／依赖缺失／打包与 CPU 预算有报告 |
| VC-01 | Pitch 流式与保持时长 | NF-01／03 | F0 比例、时长、块一致／立体声相干 |
| VC-02 | Formant／Throat／Gender／Preserve／Timbre | VC-01 分析接口 | F0／包络独立测试、辅音与中性恒等 |
| VC-03 | 8–32 带 Vocoder | NF-03 | 频带／载波／清音／释放／无输入泄漏 |
| VC-04 | Voice 组合与 Audition／真实 F0 分析 | VC-01..03，NF-02／04 | 五项交互链＋所有宿主位置端到端 |
| SM-01 | Ring 四模式与相位 | NF-03 | 理论边带、平滑、mono／stereo、块一致 |
| SM-02 | Granular／Freeze／seed 与有界缓冲 | NF-03／05 | 最高 pitch／feedback／粒数边界、任意区间重放 |
| SM-03 | 四类失真、Tone／Bias／Resonance、oversampling | NF-03／06 | 响应／DC／alias／群延迟／极值 |
| SM-04 | 四种 topology、global mix／gain／LIM | SM-01..03 | unity／串并联／拓扑切换一次推进 |
| E2E-01 | 两 FX 在轨／clip／Bus／Master＋成片／Stem 共图 | R02／03／05，所有 DSP | PCM 对齐、所有参数与数据恢复闭环 |
| E2E-02 | 真素材、长跑／性能／取消／自然退出与发布证据 | E2E-01，NF-06 | 无未处理 P0，限制在 UI／preflight 中可见 |

开发顺序建议：先 NF-01／NF-02 建立参数与宿主契约，同步 NF-03；Ring／Dist／基础 Vocoder 可先做数值原型，Voice Pitch／Formant 与 Granular 独立做质量验证；随后统一 NF-04／05，最后 E2E 两项。无需等 R12 多声道才能发布诚实的 stereo 版本，但不能跳过 PDC／版本／导出一致性。

### 9.2 建议新增或扩展的测试

| 层级 | 测试输入／动作 | 核心断言 |
|---|---|---|
| 参数／模型 | 所有可见控件、26／31 字段、LIM／seed／版本 | 每字段至少一项独立声音或状态判据；默认与 demo 区分；未知数据无损 |
| Qt 交互 | 拖旋钮／XY／节点／推子、滚轮、双击、预设／Random／A-B | 一手势一命令；程序回填零提交；总旁路不改子开关 |
| 身份 | 两个相同 Voice／Mangler，clip 与 track 调序、复制、删除 | id／seed／grain 历史不串；打开旧窗不复活已删实例 |
| Pitch／Formant | 正弦、独立 source-filter 合成语音、带真实辅音的授权人声 | F0 与 formant 独立；时长不变；无声／瞬态无伪 F0 |
| Vocoder／Ring | 频带脉冲／扫频、已知 modulator／carrier | 理论频带／边带，载波不因窗口开关变相位 |
| Grain／Freeze | same seed、不同 seed、44.1／48／96k、500ms／80Hz／高 pitch、95% feedback | 预分配有界；无越界；Random／Freeze 资源可重放 |
| 非线性 | 正弦、DC／不对称信号、极值／oversampling 档 | drive 单位、DC、alias 改善、质量 latency 正确 |
| 时间 | impulse 分 dry／wet／Send／Bus；随机 block；非零选区／seek／loop | 补偿误差≤1采样；状态重建与从头裁切一致，不只“同一输出长度” |
| 三路径 | 实时 capture PCM、真实 TimelineExporter、实际 Stem writer | 同种质量／seed／范围对齐后建议≤1e-5；后端非精确可重放时预先声明独立质量判据，不能任意放宽 |
| 工程 | preset／snapshot／MacroCommand／保存重开、Freeze asset丢失 | 全字段可恢复；未知版本保留；缺资源导出明确失败 |
| 生命周期 | pause／stop／自然空段、关窗100次、删轨／换工程、取消导出／自然退出 | 表计和颗粒清理；旧 generation 不回亮；无线程／asset／native handle 泄漏 |
| 性能 | 1／8／16轨，两复合 FX＋现有五 FX、Bus／Master | 每质量档 p50／p95／p99、underrun、latency、peak RSS；窗口开关只改变分析开销 |

拟新增测试可按 `test_voice_changer_dsp.py`、`test_spectral_mangler_dsp.py`、`test_audio_fx_latency.py`、`test_audio_fx_freeze_replay.py` 和两个 Qt adapter 文件拆分；先复用现有 registry health、clip dialog、三方 parity、Master／Bus、FX automation、preset／snapshot 的测试模式，不为可逆样式改动增加镜像断言。

端到端工程至少包含用户 `2K_h264.mp4` 的 A1、第二条授权人声／合成检源、一个 Bus、Master、两个同类新 FX、重叠／间隔 clip、Freeze 事件与非零导出选区。用户视频证明集成，人声／脉冲／正弦证明声音契约，两者都要有。

## 10. 成熟算法依据与独立参考代码

### 10.1 本次核查的第一方资料

- [Julius O. Smith：STFT 应用](https://www.dsprelated.com/freebooks/sasp/Applications_STFT.html)：频谱分析、包络／交叉合成、时间与频率变换的理论背景，用于 Voice 的分析／合成设计。
- [Röbel／Rodet，DAFx 2005：谱包络估计与 pitch shifting](https://dafx.de/paper-archive/2005/P_030.pdf)：包络估计和保持音色的算法依据；不复制其实现代码。
- [Rubber Band 官方集成说明](https://www.breakfastquay.com/rubberband/integration.html) 与 [API](https://www.breakfastquay.com/rubberband/code-doc/classRubberBand_1_1RubberBandStretcher.html)：若选择该可选后端，不能假定一次 process(N) 就产出 N；需 FIFO、查询可用输出、启动 pad／delay 和结束处理。离线／实时质量模式不得未验证就交替使用。
- [Rubber Band 官方许可](https://breakfastquay.com/rubberband/license.html)：提供 GPL 与专有产品商业授权路径；实际选用和打包前按项目发行方式确认，不因为有 Python wrapper 就推定可直接随产品分发。本计划未安装该库或改变项目依赖。
- [Csound partikkel 官方文档](https://csound.com/docs/manual/partikkel.html)：以 per-grain 调度、包络、读位置／随机分布和最大粒数为颗粒设计参考；无需引入 Csound 运行时。
- [JUCE Oversampling 官方说明](https://docs.juce.com/master/classjuce_1_1dsp_1_1Oversampling.html)：非线性过采样的处理顺序、滤波与 latency 取舍参考；不是要求引入 JUCE 或照搬其代码。

上述是公开方法和接口依据。Throat／Gender／Warp／Jitter 等宏的精确声音映射是本产品自己的设计，必须通过 fixture／听测确定并版本化，不宣称是行业唯一标准。

### 10.2 独立数学 oracle 示例

以下代码只用于解释和离线测试，不是已实现的生产 DSP；省略流式 FIFO／PDC／参数验证／滤波状态。实时重处理仍放项目可维护的向量化／编译内核。

```python
import numpy as np

def pitch_ratio(semitones: float, cents: float) -> float:
    return 2.0 ** (semitones / 12.0 + cents / 1200.0)

def mix_aligned(dry: np.ndarray, wet: np.ndarray, mix_pct: float) -> np.ndarray:
    # 调用前已做时延和shape对齐；同一输入在0/100两端应精确选中。
    if mix_pct == 0.0:
        return dry.copy()
    if mix_pct == 100.0:
        return wet.copy()
    m = mix_pct / 100.0
    return dry + m * (wet - dry)

def parallel_reference(dry: np.ndarray,
                       wet_branches: list[np.ndarray],
                       mixes_pct: list[float]) -> np.ndarray:
    # 只传已启用模块的wet-only结果，mix=0的已启用模块仍计入N。
    if not wet_branches:
        return dry.copy()
    if len(wet_branches) != len(mixes_pct):
        raise ValueError('branch/mix count mismatch')
    residual = np.zeros_like(dry)
    for wet, pct in zip(wet_branches, mixes_pct):
        residual += (pct / 100.0) * (wet - dry)
    return dry + residual / float(len(wet_branches))

def sine_ring_reference(x: np.ndarray, carrier_hz: float,
                        sample_rate: float, start_sample: int) -> np.ndarray:
    # 固定频率oracle；实际变频需累积phase并平滑，不能重算t*f产生跳变。
    t = (start_sample + np.arange(x.shape[-1])) / sample_rate
    return x * np.sin(2.0 * np.pi * carrier_hz * t)[None, :]
```

参考判据：`parallel_reference(x,[x,x,x],[100,100,100]) == x`；`mix_aligned(x,y,0)==x`；+12st 的 ratio 为2；Ring 的正弦频谱有 f_input±f_carrier。算法单测用这些独立关系验证，不能拿 UI 的模拟曲线当真实音频 oracle。
