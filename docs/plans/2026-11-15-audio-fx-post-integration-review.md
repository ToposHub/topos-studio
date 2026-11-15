# Audio Mixer 与七款效果器：接入后复审和下一轮修复计划

- 日期：2026-11-15；输入提交：`22e6e1c8`，复审开始时工作区干净。
- 对照：[上一份计划 §1–§11](2026-11-15-audio-mixer-fx-review-followup.md)。检查 R01–R13、NF／VC／SM／E2E 的实际生产路径，包含原五款和新增 Voice Changer／Spectral Mangler。
- 交付：本轮小修已写入工作区；本文 H01–H13 为**仍待实施**的大问题。原计划的实施记录保留，但完成状态须结合这份复核结果阅读。
- 结论：新效果器已注册、可打开、可处理音频；仍存在影响声音、工程兼容、选区／尾音、实时速度的阻塞。现有回归通过不能代替这些独立案例的验收。
- 证据：[本轮审计目录](../audit/2026-11-15-audio-fx-post-integration/README.md)。没有重新对标 Resolve 专有声音；算法依据继续使用上一份计划 §10 的公开方法，不要求复制专有实现。

## 1. 本轮检查与验证结果

### 1.1 自动化与独立探针

- 修改前：96 个相关文件，**1719 passed、1 skipped**。
- 最终：102 个相关文件，**1796 passed、1 skipped，101.23s**；其中本轮六个新增回归文件共 **77 项通过**。
- 范围包括 DSP、实时 Mixer、真实 TimelineExporter／Stem、Master 限幅／自动化、条带状态、窗口、设备采样率和录音相关回归；不是全仓测试结果。
- 唯一跳过是旧波形测试缺少 WAV assets，具体条目在 JUnit／证据 README。
- 独立探针另行保留大问题的 actual／expected，不加入“全部通过”计数。部分原测试只验证同块长、常值曲线、稳态或“不抛异常”，无法覆盖本报告缺陷。

### 1.2 完整主窗口实播和对照

使用 `tests/test_videos/2K_h264.mp4`，从该视频提取 12 秒音频为临时 WAV。完整 MainWindow 中有视频关联音轨及另一条挂 FX 的普通音轨；Voice 开启 Formant／Pitch，Mangler 开启 Dual Ring／Dist。实际 AudioSink 播放约 8 秒，打开两个真实编辑窗口；临时配置关闭自动保存。

| 指标 | 两款新 FX | 相同素材、不挂新 FX |
|---|---|---|
| mix_samples p50 | 71.97ms | 1.91ms |
| mix_samples p99 | 153.22ms | 7.59ms |
| mix_samples max | 189.46ms | 13.20ms |
| 音频时钟／墙钟 | 3.56s／7.74s，**0.460x** | 7.96s／7.95s，**1.001x** |
| mix 调用数 | 91 | 203 |
| 新 FX 遥测 | 43 条；Voice 输出约 −35.43..−16.62dB，Mangler 约 −23.67..−9.63dB | 不存在新 FX 实例，收集器不计此列 |
| 暂停后窗口电平 | Voice −55dB 地板，Mangler −60dB 地板 | 无新 FX 窗口 |

两次 ring 的 underruns／overflows／dropped 都是 0，**不能据此判定实时播放达标**；音频时钟已揭示新 FX 配置明显跟不上。测试按先后顺序运行，无宽回归并发干扰；这是本机、该配置的基准，不外推为所有设备的固定倍率。探针使用已有 benchmark 的强制进程退出，不证明自然退出或录音清理正确；本轮没有作盲听质量评价。

## 2. 本轮已直接修复的局部问题

| ID | 原问题 | 最终行为／验证 |
|---|---|---|
| L01 | 编辑新 FX 时 nested→flat 转换把 random_seed 重置为 0，未显示字段丢失 | Dialog 保存 base 参数，只有映射的可见声音字段被覆盖；seed／未知字段保留，回填后继续编辑也一致 |
| L02 | preview timer 每次重启，连续拖动期间不预听；回填后旧 timer 仍提交；关窗丢最后一笔 | preview 改节流；外部回填取消旧 pending；模型无 Cancel 按钮的 modeless 窗口关闭前提交最后待处理编辑一次；精确手势边界仍见 H11 |
| L03 | 生产窗口初始显示演示 F0／电平，静音事件返回不清表 | 无测量初始为零／地板；静音清 F0、谱、activity、peak／clip；confidence 不再标为测得的语言可懂度 |
| L04 | Mangler Dialog 调不存在的 widget.setTelemetry；零 RMS 不清电平 | 调真实公开 update_telemetry；wave／grain 数据可到 visualizer 缓存，零值和 reset 清理有效；**真实绘制仍未完成**，见 H11 |
| L05 | Mixer 给所有 Voice 模块伪造 active=True，F0 没有真正经过实例 tap | 可选 processor.collect_telemetry → tap.analysis → TrackLevel／播放线程 → 对应窗口；使用实际模块开关和 F0；peak dB 不再冒充 RMS。clip 通道／完整谱和 grain 描述仍见 H11 |
| L06 | 编辑、旁路、调序及 DY 编辑重建实例，默默把 legacy／future 版本变 current 并丢顶层 extras | 16 个 Mixer 同实例入口和 Dynamics._write_instance 使用 replace；不相关实例保留原对象；真正新建仍 current。已知参数内部未知字段仍有缺口，见 H01 |
| L07 | 先检查旧 is_active 再采自动化，静态中性效果永远不能被曲线唤醒 | 轨／Bus／Master 的实时和 renderer 六处允许 sampler 先更新；drive=0→70→0→70 可唤醒、旁路、再唤醒 |
| L08 | Voice reset 调同值 set_latency，不清 dry FIFO | reset 真正清空缓存和分析；预热后 reset 的旧音频残留峰 0.4→0 |
| L09 | Pitch Link 对共享双声道 WOLA 每个 mono 调一次，引入串声 | 每声道只推进自身 WOLA 一次；仅左输入时静音右声道峰 0.3597→0；Link 声音方向仍见 H05 |
| L10 | Vocoder 包络／清音滤波 zi 丢失，绝对时间和累计 phase 双计时 | 持续 filter 状态与振荡相位；carrier 每声道每块生成一次供频带复用；whole／480块 Noise 误差 0.02324→0，Saw 0.07438→约2.45e−14 |
| L11 | Dual／FM-Ring 将 wrapped 主相位乘非整数，块边界重新跳相 | 独立载波相位跨块连续；Dual block delta 0.4631→约6.47e−10，FM-Ring 0.6480→约1.51e−9 |
| L12 | Grain reset 不重置 PRNG，换采样率后外层 seed 缓存阻止新实例设置原 seed | 同 seed reset／采样率重建与新实例重放差均为 0 |
| L13 | 只有 LIM 开启时 is_active=False，保护被整链跳过 | LIM-only 也激活；总旁路仍直通 |
| L14 | routing 变化影响 global_mix=0 的 dry，单采样块触发广播异常 | 只淡化 wet，fade 进度跨块延续；1-sample 块不崩溃；typed／dict 参数行为一致 |
| L15 | Master 末端“硬保护”按整块峰缩放，未来峰压低较早声音 | 改逐采样、声道联动 guard，保留已有 attack／release 包络。新增因果／随机分块／声道比回归；原 Spectral 三路径测试恢复通过，**未放宽其容差** |
| L16 | BitCrush 测试对 1 秒输出切 `[48000:]`，空数组使量化断言永远通过 | 原语处验证非空、有限均匀量化网格，完整 DSP 输出另验证 shape／非零；滤波后连续输出不错误断言为离散电平 |

新回归分别为 `test_audio_post_integration_{voice_regressions,mangler_regressions,automation_wakeup,master_guard}.py` 和 shared 下 `test_audio_post_integration_{dialogs,metadata}.py`。修前失败、修后通过与最终宽回归均在审计目录；不把其中所有 77 项都宣称为原基线必失败，有些是新增保护条件。

## 3. 上一份计划完成状态复核

“部分”不是否认已有实现，而是原任务还有关键验收未达到。下表优先指出影响声音／数据的门槛。

| 原任务 | 本轮判定 | 依据／后续 |
|---|---|---|
| R01 | 部分，P0 | 顶层 unknown/version 可保存，L06 修编辑丢 metadata；v2 错配 legacy、future 被标 supported、非 EQ 版本分发不足，H01 |
| R02 | 部分，P0 | 普通轨单调推进已改善；Bus／Master 动态曲线、非零选区预卷仍失败，H02 |
| R03 | 部分，P0 | Stem 求和／pre-send 基础有回归；clip tail 和 Bus／Master 时序仍不同，H02／03 |
| R04 | 部分，P0 | 有 include_fx_tail 开关，但漏 Track／Clip，串联预算取 max；Voice latency/tail=0，H03／04／05 |
| R05 | 部分 | 显式空值、Master snapshot、常值自动化有覆盖；变化曲线成片／Stem失败，L07只修唤醒，H02／12 |
| R06 | 部分 | 窗口回填基本存在；L01／02／06 补种子、pending与metadata；完整手势／双层旁路／clip preview见H11 |
| R07 | 已有 focused 覆盖，发布条件未齐 | Delay双读头平滑已有；历史算法兼容不覆盖全部变更，H01 |
| R08 | **部分** | 无反馈快路径有效；0.01ms、FB30%仍约88.85ms／2000 samples，H13 |
| R09 | 已有 focused 覆盖 | 新三实例组后增益／legacy None 语义已有，L06保留DY版本；全面旧工程改声仍依赖H01 |
| R10 | 部分 | 设备rate通知有回归；arm／record／monitor分离和退出收尾是未完成软件任务，不全是硬件验收，H13 |
| R11 | 部分 | L05接实际基础分析；完整DTO／clip实例tap／真正wave／粒子绘制未完，H11 |
| R12 | 部分 | 当前真实DSP主域仍stereo；Panner环绕舞台与能力口径需明确，H13 |
| R13 | P2未实施 | 继续独立排期，不阻塞正确的stereo基础版本 |
| NF-01 | 部分 | type／schema注册和默认值可用；finite／seed坏值／自动化metadata白名单／未知参数仍缺，H01／12 |
| NF-02 | 部分 | 用户UI可打开、L01–06改善；400ms停手不等于完整手势，Audition与用户库未接真实按钮，H11 |
| NF-03 | 未达到 | 内部计数器不等于绝对时间context；Pitch Mix内部也未对齐，图级PDC／clip id仍缺，H02／05 |
| NF-04 | 部分 | L05把实际scalar送达窗口；input/output谱、wave／grain描述与clip分发仍缺，H11 |
| NF-05 | 部分 | 库函数可保存seed，不等于UI保存按钮持久化；Freeze资产／事件历史未实现，H09／11 |
| NF-06 | 未达到 | 实际新FX音频只有0.460x；依赖故障／打包／质量档报告不足，H12／13 |
| VC-01 | 未达到 | 无界Pitch历史、内部Mix回声、强alias、精度低于原目标，H05 |
| VC-02 | 未达到 | 首样本单位脉冲被WOLA完全吞掉，边界不是稳态误差问题；包络测试容差20%也未达原建议5%，H04／06 |
| VC-03 | 部分 | L10修块状态；vocoder_formant_st仍无消费，清音／频带品质需验，H06 |
| VC-04 | 部分 | 基础链可运行；Audition／F0／真实分析和全生命周期仍缺，H06／11 |
| SM-01 | 小修后已有 focused 覆盖 | L11补Dual／FM相位；仍需高频带限及音质／性能，H13 |
| SM-02 | 未达到 | grain调度、读头、历史生命周期、feedback变块失效、Cloud=Scatter，H08／09 |
| SM-03 | 未达到 | Dist共享左右状态、31采样块尾补零、BitCrush hold重置，H07 |
| SM-04 | 部分 | L13／14修活跃与路由边界；分支时延、MiniLimiter包络、Freeze仍缺，H09／10 |
| E2E-01 | 部分 | 当前固定配置／块长测试通过；跨块、跨选区、clip tail和变化自动化仍有反例，H02／03／07／08 |
| E2E-02 | 未达到 | 本轮已补真实主窗测量，但结果尚不满足实时要求；硬件／自然退出仍待，H13 |

## 4. 新任务总表及依赖

P0：错误声音、交付不一致、数据／版本丢失；P1：核心能力或可靠性；P2：扩展。没有通过的关键门槛不改写成更宽阈值后标完成。

| ID | 优先级 | 工作包 | 前置与首要验收 |
|---|---|---|---|
| H01 | P0 | 精确版本迁移、opaque参数和future能力门控 | 冻结真历史fixture；v2 PCM与真历史一致 |
| H02 | P0 | 真正分块的共享图、绝对时基、Bus／Master自动化和预卷 | H01；变化曲线与非零选区恢复正确 |
| H03 | P0 | 全路径尾音／latency flush及导出长度 | H02；Track／Clip／串联Bus→Master尾音不截 |
| H04 | P0 | Voice WOLA边界和Neutral恒等 | H01版本策略；首尾脉冲、短输入全部重构 |
| H05 | P0 | Voice Pitch有界流、时延／Mix、抗混叠与Link | H02／04；时长／F0／包络／内存独立验证 |
| H06 | P1 | 声音参数有效性、Vocoder formant、清音和F0置信度 | H04／05；可见参数逐个声学oracle |
| H07 | P0 | Dist逐声道状态、正确过采样及sample-hold | H01／02；静音声道不串音、无块尾补零 |
| H08 | P0 | 颗粒事件调度、读头／历史年龄、feedback和模式 | H02；grain数、读位置及声音不依赖块长 |
| H09 | P0／P1 | Freeze捕获资产、逐事件历史与重放 | H02／08；保存重开／任意区间复现同冻结内容 |
| H10 | P1 | Mangler内部Limiter、拓扑切换与固定延迟 | H02／07／08；release按采样推进、分支对齐 |
| H11 | P1 | 完整UI手势、预设／Audition和真实分析图／clip通道 | H02；真实手势、按钮和Qt消费端到端验收 |
| H12 | P1 | 参数校验、自动化eligibility、后端故障／资源诊断 | H01／11；坏值不污染PCM，metadata不进数值lane |
| H13 | P0／P1／P2 | 实时性能、长跑、硬件／退出与发布证据 | 全部P0先收敛；音频时钟约1x且p99有预算余量 |

推荐顺序：H01先冻结兼容基线；H02与H04／07的接口设计可并行；随后H03／05／08；再做H06／09／10／11／12完整状态与UI；H13性能应从原型阶段持续测，不能最后才发现算法无法实时运行。

## 5. 详细实施步骤和验证

### H01：真实版本边界与无损编辑

**已复现：** 真历史提交 `302b88b6` 的 ALGORITHM_VERSION=2已经使用修正后的Butterworth。HPF 1000Hz／12dB、250Hz正弦：历史RMS=−27.13282dB，当前显式version=2为−51.25536dB，相差24.12254dB。`LEGACY_ALGORITHM_MAX=2`把两个不同语义归为一类。已知类型version777的diagnostic虽然compat=future，却status=supported、reason为空。

**位置：** `core/fx_versioning.py`、`timeline_v2.py`、`audio_dsp.py` EQ版本分发、`effect_preset.py`、模型／命令／UI参数编解码。

**实施：**

1. 从实际历史提交冻结v1/v2/v3/v4工程、预设和PCM；逐效果记录改声边界，不用一个全局“≤2”猜所有处理器。
2. 无版本的旧数据有历史歧义，结合工程schema／创建版本／迁移来源决定；无法判断时保留原始payload并给明确兼容选择，不悄悄套当前声音。
3. 兼容处理不只EQ：Delay平滑、Dynamics／Make Up、Reverb等历史声音变化分别分发，质量／算法升级走command和可撤销状态。
4. 所有编辑链路复用原实例或replace；L06仅修本轮确认的Mixer／DY入口，继续扫command、preset、clip和参数dataclass的未知字段。参数内部unknown不能因已知Params.to_dict而丢失。
5. future version标unsupported/degraded并说明原因；预览与导出同一能力判断，导出前要求明确的降级策略。只打日志不等于用户已经选择丢弃或改声。

**验收：** v2案例与历史PCM一致；future777 roundtrip无损且不能被误报完整支持；未知类型夹在两个已知实例中不丢整链；编辑另一个FX不改变旧版本；升级／undo／保存重开能逐字段和PCM核对。

### H02：共享音频图的样本时基和预卷

**已复现：** Track曲线f0=0、f10=−12dB能正确运行；相同曲线在Bus／Master成片和Stem的f20仍输出约0.2，应0.0502377。因为后级对整段或较大chunk只按start_frame采一次。Delay从头渲染再裁f4..10有峰0.3440148，直接导出f4..10全零，缺少区间前状态。

**实施：**

1. 统一block-major调度：一块内完成clip求和→轨→依赖Bus／SC→Master，下一块再前进。不要只给track一个单调游标，后级仍整段一次调用。
2. 给所有node传absolute sample cursor、采样率、generation、discontinuity和控制事件；新FX内部计数器从0开始不是任意seek位置的绝对时基。
3. 自动化按同一sample坐标求值；连续参数ramp或DSP平滑，离散事件按块内offset切分，Bus／Master不例外。L07只修“中性唤醒”，没有修长段曲线采样。
4. clip entries补effect_id，贯通所有tuple解包／sampler／缓存／tap。编译主路、Send和SC依赖，定义循环处理；tap只活跃在所需窗口，长工程不保存全时长副本。
5. 非零In／seek／loop从可用checkpoint或确定预卷重建，然后裁出范围；对随机、反馈、Freeze声明恢复策略。不能只减latency样本而忽略网络历史。
6. 导出／Stem复用图和同质量／seed；取消、异常释放decoder、native handle和tap，避免另外复制一份简化路由。

**验收：** Bus／Master动态曲线到f20≈0.0502377；随机block／分数fps／非零In与从头裁切≤约定误差；同节点同sample只推进一次；SC在Track／Bus／Master时间对齐；clip同类调序不串状态；内存随block×活跃源受控。

### H03：全路径尾音和输出长度

**已复现：** 200ms Track Delay在include_fx_tail模式仍只输出160ms且全零；Bus200ms→Master200ms只延到400ms，400ms回声恰被截断。clip Delay成片峰0.3440148，实时／Stem为0。Voice末端脉冲下一零块有0.50002，节点却报告tail=0。

**实施：**

1. 分开algorithmic latency、创作延迟／tail、剩余drain和是否无限持音；即使无创作tail，FIFO／WOLA／滤波剩余内容也要flush。
2. 汇合取各路径的最新可能输出时间，串联累计，不能对所有Bus／Master简单取max。包括clip→track→嵌套Bus／Send→Master，而不只Bus和Master。
3. 实时、成片、Stem都给已结束clip的FX持续喂零；track输入为空时其自身和下游仍按状态推进。零输出块不能立即判定长Delay没有尾音。
4. 导出strict／include-tail的样本长度、视频末尾延展和进度一致；防止按毫秒／帧上取整后把边界的首回声截掉。达到硬预算截尾要可诊断。
5. Freeze无限持音不伪报固定4s；严格范围按用户选择结束，含尾需要明确上限／取消策略。stop清旧尾，pause／seek按约定冻结或重建。

**验收：** 单Track／Clip的200ms回声出现；Bus200→Master200首回声完整保留在400ms附近；三路径clip tail一致；Voice尾端脉冲不丢；strict长度不变，include-tail有正确能量与长度；任意分块／采样率／顺序输出一致。

### H04：WOLA边界、窗归一与Neutral

**已复现：** 默认Neutral、输入4096采样的首样本单位脉冲，输出peak=0／energy=0，报告latency=1024。Hann起始样本为0且未补齐负时间窗，稳态COLA常数不能重构首边界；只测试中段正弦会漏掉。

**实施：**

1. 明确analysis／synthesis窗、hop和因果latency。用前置padding／虚拟负时间帧覆盖首边界，末尾zero-pad并drain覆盖尾边界；按OLA有效权重处理边界，不只除稳态常数。
2. 构建输入／输出FIFO和窗口状态，容量有界；短输入、1sample输入也能结束flush；不得在队列不足时随意补零掩盖调度错误。
3. 中性路径应在报告的相同latency后精确直通或在明确误差内重构；Formant mix=0、100和禁用不能各有不同无报告时延。
4. 谱包络估计／倒谱lifter保证合理的实信号对称性、floor与归一；DC／Nyquist／静音不导致除零或宽带增益爆炸。

**验收：** impulse遍历首样本、每个hop相位、最后样本；输出等于延迟后的输入而非全0；1／小于窗／随机长度、L-only／R-only、整块／随机块、reset／采样率切换；首尾能量和采样数都核对。不得用删除开头一窗／尾部若干样本后再宣布Neutral恒等。

### H05：Pitch有界流、时延／Mix、抗混叠和Link

**已复现：** `_bufs`按累计写样本扩容：处理480采样时每声道31680容量，处理48000后69184；持续增长，float64 stereo单历史约2.575GiB／小时的结构性外推，且重复复制。0st／50% mix脉冲在sample0和5280各出现0.25，latency却报0，内部已出现110ms双重拍。20kHz输入+12st强alias到8kHz，RMS≈0.23696。

**实施：**

1. 换成真正有界的streaming Pitch后端：沿原计划比较WSOLA＋有状态带限重采样或锁相phase vocoder；不靠修改clip速度冒充保持时长的变声。
2. ring／FIFO按最大读年龄、插值kernel和预读需求分配，绝对读／写索引和物理槽位分开；不能把数组长度随整部片长增加当作“防覆盖”。
3. 正确报告Pitch基线时延，包括0st和首次开启Link；dry、wet、旁路和其他Bus／Send支路都补齐。quality变化用受控重建／淡化，不让时间线相对视频跳动。
4. 降带宽在产生重采样alias之前处理；后置低通无法删除已经落入8kHz的折叠。验证不同倍率／采样率的阻带和音质，再决定可用范围。
5. 按原契约实现Glide；Pitch Link方向统一为0保当前谱包络、100随pitch，fine cents也计入。现有代码负向shift×link与契约不符，不能仅修名称。
6. pitch／jitter变化由sample clock平滑；seed与generation重放，左右共用时间映射但不共用有状态滤波器；默认copy保声音，显式新seed才改随机性。

**验收：** 0st Mix0／50／100只有同一对齐脉冲；±24st／fine端点有限；原±12st目标≤5cents保留，达不到须明确质量限制而非把原标准改25cents；20kHz +12st不出现当前强8kHz折叠；长期buffer字节保持有界；参数变化／seek／块划分不改变时间。

### H06：逐参数有效性、Vocoder与可靠分析

**已复现：** `pitch_glide_ms`、`pitch_tracking`、`vocoder_formant_st`从端点切换输出最大差均为0；Cloud／Scatter另见H08。F0固定70–500Hz且只分析左声道：880／1760Hz都报440.367Hz、confidence≈0.954；仅右声道人声报无声。

**实施：**

1. 按上一计划26／31参数表建立逐控件清单，标明DSP消费者、单位、宏映射、可用模式和独立验证信号；无效控件先明确禁用，不能仅有序列化字段。
2. Vocoder formant移动合成频带／包络轴，与carrier基频分离；bands／carrier变更的滤波组重建平滑、有界；不让8→32带自动大幅改响度。
3. Preserve／Unvoiced按清音／瞬态／残差处理，而非无条件把高通全声道重复叠入；Natural／Crisp、Throat／Gender的宏映射用合成声学fixture与听测校准，默认与边界行为写版本。
4. F0以左右能量／相关性选参考或联合分析，反相不抵消；Vocal／Wide实际控制分析策略；无可靠基频显示N/A，防八度错误和高置信假阳性。
5. 输入和输出F0分别测量，不能仅乘理论倍率；CONF明确是检测置信度，不是语言可懂度或声音质量百分比。分析昂贵工作按消费者和预算调度。

**验收：** 每个可见有效参数有至少一项非零且方向可解释的声音变化；formant-only保F0、pitch-only保／联动包络符合Link；声码器formant不改变carrier pitch；右独有／反相／高频／多音／噪声／清辅音的validity正确；分块、采样率和窗口开关不改变PCM。

### H07：Dist逐声道状态与正确过采样

**已复现：** 左正弦、右静音时，右输出峰Tape≈0.00368、Fold≈0.01967、HardClip≈0.00516、BitCrush≈0.24277；FIR history／up／down／tone状态跨左右共享。整块／256块误差最高约2.142，每块尾31采样被补0。两级FIR群延迟相加，不会自动抵消。

**实施：**

1. FIR上／下采样历史、tone zi、sample-hold phase／held value分别按声道维护；只共享不可变系数，不共享处理状态。为mono／stereo／rate change定义reset。
2. 重写有状态polyphase流程：插值→非线性→抗混叠低通→抽取，保留抽取相位；输出长度由FIFO／latency管理，不裁未来样本后拿0补块尾。
3. 报告并补偿两级滤波群延迟；Tone／dry／global mix／并联都对齐。改sample rate／quality时受控切换，不让状态污染另一声道。
4. BitCrush hold跨块保相位和值，量化网格／位深与hold-rate的参数映射明确；量化原语和滤波后波形分别验收。
5. UI waveshaper图使用同源响应函数和真实dB定义；非线性之前Bias、之后DC blocker的顺序和响应必须可解释。

**验收：** 静音右声道严格不因左输入产生声音；长正弦无固定块尾空洞；整块／1／255／257／1920／随机块≤约定数值误差；hold长度跨块连续；扫频alias有量化改善；实测IR群延迟与reported latency一致；所有curve／tone／bias端点finite。

### H08：真正事件驱动的Granular

**已复现：** 1秒块／80grains/s／500ms先触发80粒后才渲染，64池满丢16，物理最大重叠其实只有40；+12st／85ms／position0仅回看2samples，约99.975%读头被钳到同一点；合法Reverse seed19需要5.2617s历史，容量只有5s；255／257交替块时FB95%和FB0输出差0；Cloud=Scatter差0。

**实施：**

1. 在每个sample事件边界交错“释放结束voice→触发→渲染到下一事件”，不能整块先触发全部未来grains；事件序号不依赖block数量。
2. 读头以完整输出粒长、倍率、方向和writer推进计算合法起点。Reverse年龄 `D+(r+1)*t`，forward也核验整段读范围；不能用clamp把不合法的位置变成一个常量采样。
3. history容量覆盖最大生命周期读取年龄＋position／spray回看＋block／插值余量；动态变化不覆盖尚被voice引用的数据。不足历史明确零填／延后，不能读未写或复用垃圾。
4. feedback改为sample／明确定义固定延迟的环路，有界湿声归一、DC和稳定性保护；当前“上块shape相同才反馈”必须移除。若保留固定创作延迟应按采样定义，不能等于宿主块长。
5. Cloud／Scatter定义不同的事件／位置分布并真正实现；每种随机量由seed和独立event stream决定；调序／复制／重放不串voice历史。

**验收：** 该80粒案例不因一秒块丢16粒；整块／随机块事件列表一致；读头实际遍历合法源数据而非99.975%钳住；所有允许参数／seed的read-age不超过capacity；FB95%在交替块仍有效且稳定；Cloud／Scatter有契约规定差异，同seed重放一致；peak RSS／voice pool有上限。

### H09：Freeze内容、事件和资产事务

**状态：** 目前只有进程内停写，保存重开不能恢复冻结内容，原实施记录也已承认。它不能作为“预设／工程恢复已完整”的例外被忽略。

**实施：**

1. 每次Freeze分配event_id，在音频sample边界确认anchor和输入history；预分配buffer交换／有界快照交后台，不由UI读取运行中DSP或同步写盘。
2. pending即时冻结到同一anchor；成功一次command保存project-managed capture asset引用、格式／算法／调度元数据，失败淡化回滚。raw PCM不进入参数JSON或机器绝对路径。
3. 多次Freeze保留逐事件asset／anchor，第二次不能覆盖第一次来源；解除Freeze也是确定事件。选择“旧voice淡出重置”或完整voice状态快照，写成版本化声音契约。
4. 保存重开、任意选区、Stem、undo／redo按同一事件历史恢复；资产缺失preflight明确失败；取消、复制引用和GC走项目资源所有权。

**验收：** Freeze→保存→退出→重开PCM一致；两个不同冻结内容在各自历史时段正确重放；导出只选第二段也能恢复；捕获取消／写盘失败／缺资源无悬挂引用；无限持音与导出范围策略符合H03。

### H10：内部Limiter和拓扑时延

**已复现：** Mangler MiniLimiter标称50ms release，但按“一块一个采样系数”恢复。相同过载后1秒quiet：block=1时gain≈1，64为0.6342，256为0.5377，2000为0.5050，48000为0.5002。L13修激活，没有修包络。

**实施：** sample-clock envelope或经过证明的等价向量算法；同一时刻的峰值、attack／release和双声道联动定义清楚。分支dry／wet与quality固定时延对齐，切routing不能同一stateful对象推进两遍。enabled改变并联N时平滑全部相关权重；临时fade不能掩盖音频时间变化。

**验收：** 所有块长的1秒release一致；LIM-only／gain>0／global_mix端点可解释；无意外削波／声像改变；串并联IR对齐；routing连续切换、1sample块、reset／采样率和Freeze组合都不崩溃。

### H11：UI真实手势、用户库、Audition和分析画布

**确认边界：** L01–05修了局部问题；仍用400ms停手计时，鼠标按住停顿时会提前commit，一次长拖可多命令。Voice Audition仅发信号；两widget用户保存按钮仍写内存dict，没有调用已测试的持久库。Mangler `_external_wave/_external_grains`仍只存不画，paint使用模拟base_sig／particles；DSP也未生产完整wave／grain DTO。clip无独立实例分析链。

**实施：**

1. 控件／XY／节点／推子发begin／preview／release／cancel，拖动期间即使停顿也不commit；滚轮合并、数值确认、preset／Random／A-B各一条原子命令。modeless关闭保最后编辑，删除／换工程取消旧pending。
2. 用户库按钮真正调用effect_preset服务；列表合并内置只读和用户可改；seed／版本／未知字段往返；错误提示不吞整库。A/B是否session和Copy不改当前音频的契约一致。
3. Audition接现有transport／当前片段或选区，未实现时明确禁用；不启动第二套AudioSink或无来源的假人声。
4. 统一实例enabled与params.enabled的有效状态和UI显示／回写，避免右键旁路后窗内power仍显示激活；外部undo／preset回填不产生新命令。
5. processor生产有单位／valid／sample cursor／generation的限长analysis快照；真实输入／输出谱、保峰抽稀wave、active grains有真实消费分支。无数据显空／N/A，参数示意图与实时波形明确区分。
6. clip `(clip_id,effect_id)`独立tap→clip manager→窗口，不能复用轨末端谱；订阅控制FFT/F0预算，关闭窗口不改变声音；空段／暂停／seek清理迟到事件。

**验收：** 用真实鼠标拖动、按住停1秒再继续、release、Esc／关窗、滚轮、输入框和菜单测试，不手动调_emit_commit代替；真实点击保存→重启→列表恢复；Audition有正确音源；不同频率／gain的两个同类实例和重叠clip分别显示；100次开关无对象／订阅增长；小屏缩放不裁控件。

### H12：参数合法性、自动化白名单和后端能力

**已复现：** 两个Params.from_dict保留NaN gain；Voice该值处理1920×2产生3840个非有限输出。bad seed字符串直接ValueError。数值范围留给DSP零散clip，不能保证序列化／UI／lane的一致性。仅按int／float枚举参数会让seed等元数据进入曲线。

**实施：** registry schema成为校验和automation_kind真源；有限值／范围／enum／整数／bool校验统一，错误保留原始payload并诊断；支持范围内可纠正的值采用可解释规范化，禁止NaN进入DSP或QPainter。metadata／资源引用never，bands／routing／Freeze为event，连续参数有单位和ramp域。逐处理器、质量档、版本和布局建立preview/export能力，模拟SciPy／native后端缺失，不能一个新类导入失败拖垮整库或静默跳过声音。

**验收：** 全字段NaN／Inf／错类型／越界矩阵，保存重开不污染PCM；future字段无损；seed／版本不出现在普通数值lane；负半音／bias范围正确；坏资源／缺依赖有相同诊断和导出策略；安装包在目标平台可加载实际后端。

### H13：实时性能、设备与最终发布门槛

**实测：** 本轮两新FX音频0.460x，p50≈71.97ms，40ms视频帧音频块预算已不足。无FX对照1.001x，p50≈1.91ms。Delay无反馈快路径有效，但0.01ms／FB30%仍约88.85ms（2000样本预算41.67ms，非隔离测量），不能把R08标全参数完成。

**实施：**

1. 分profile拆开F0／FFT／Pitch逐采样Python循环、Grain逐粒、Dist过采样和分析；区分冷启动、稳态、参数更新／seek，固定素材、块长、quality和机器环境。
2. 需要递推／逐采样的热点移到可维护编译内核／有证明向量路径；预分配、声道独立状态、缓存窗和filterbank，不在audio callback反复扩容／重新生成每频带相同carrier。
3. 不通过禁用声音参数、提高块长隐藏交互延迟或减少采样率冒充优化；若先提供离线／预渲染质量档，UI和导出明确能力与声音差别。
4. 1／8／16轨、七FX、Bus／Master、44.1／48／96k分别报告p50／p95／p99、音频时钟／墙钟、sink／ring、端到端latency和peak RSS；`underruns=0`单指标不够。
5. R10补arm／record／monitor的软件状态分离、输入监听契约、磁盘失败和自然退出收尾；它们不是全部等硬件才可开发。真机热拔插／take恢复另外验收。R12先明确stereo能力，真实多声道另做；R13 VCA／True Peak等P2按产品排期。

**最终门槛：** 所有本报告P0案例转成原基线会失败的回归并通过；无隐性丢声／future改声；实际音频时钟约1x，p99留出解码／输出预算（建议DSP≤块预算50%，按目标硬件冻结）；保存重开、非零选区／Freeze、真实主窗、成片／Stem、取消／自然退出都有证据。剩余P1限制由产品明确接受或继续修，不能只改计划状态为✅。

## 6. 复现、证据和每批交付

主要探针均位于审计目录，可用仓库`.venv`重新执行；只生成临时合成音频，不修改用户工程／预设。

| 文件 | 内容 |
|---|---|
| `voice-probe.py`、`voice-before.json`／`voice-after.json` | WOLA边界、Pitch延迟／内存／alias、死参数、Vocoder／串声小修和F0 |
| `mangler-probe.py`、`mangler-before.json`／`mangler-after.json` | Ring、Grain调度／读头／feedback、Dist串声／块尾、MiniLimiter |
| `state-export-probe.py`、`state-export-baseline.json`／`state-export-after.json` | 真实导出／Stem／Mixer尾音、变化曲线、非零In和真历史v2 |
| `mangler-master-limiter.json` | L15修前定位：pre-Master差0，post差0.08230；独立future峰问题 |
| `master-guard-after.json`、新guard测试 | L15修后因果和块一致结果，不再对整块归一化 |
| `real-app-probe.py`、`real-app*.log/json` | 真实视频MainWindow有／无新FX顺序对照；会打开独立测试窗口和AudioSink |
| `parameter-validation.json`、`delay-feedback-hotspot.json` | 坏参数与带反馈Delay热点 |
| `final-test-files.txt`、`final-regression.log/xml` | 最终确切回归范围、通过／跳过及耗时 |

每批实施先冻结失败基线，再小步实现、Review、focused回归和对应三路径／状态测试；保留独立oracle。不得把参数字典变化、不抛异常、同一函数自比或空切片断言当成声音实现。最终测试日志、产品能力和本文任务状态需一起更新。


---

## 7. H01–H13 实施进度（2026-11-16 第二工作批次）

输入提交：`51b3c8ee`（本计划 + L01–L16 已入工作区后提交）。每完成一项
独立提交；回归全绿后写状态。**没有把未达项改写成完成。**

| ID | 状态 | 交付要点（对应 §5 验收） | 提交 |
|---|---|---|---|
| H01 | ✅ | 三分桶 EQ（v≤1 legacy / v2=302b88b6 精确 / v3+ 现行）对齐冻结历史 PCM（真历史模块渲染基线，audit/h01/）；Delay v≤3 直接切读头；未标注→0 入诊断；future→degraded+reason、导出 preflight 无授权阻断/`allow_algorithm_degraded` 放行；10 类参数 dataclass 保留未知键；`UpgradeTrackEffectAlgorithmCommand` 可撤销升级 | 0459ddf0 |
| H02 | ✅ | Bus/Master 逐帧块内自动化（变化曲线 f20=0.050233≈理论值）；非零选区确定性预卷（选区==从头裁剪，峰值逐位一致）；分数 fps 精确长度 | cef420bb |
| H03 | ✅ | include_fx_tail 全路径预算（轨+沿途 Bus+Master 串联累计）；轨/clip 尾音 drain 延入扩展区；Stem 长度同预算；实时已结束 clip 链喂零——clip tail 三路径 export/stem/live 全 0.3440148；Voice/Mangler tail 报告真实 flush 需求 | 639c7bff |
| H04 | ✅ | WOLA 输入前导 win−hop + 周期 hann：首样本脉冲重构 0.99995@latency、中性恒等 5e-16、flush() 尾边界、逐 hop 相位能量守恒；包络改峰值连线（±6st 位移误差 21%→6%） | 333528d5 |
| H05 | ✅ | Pitch 有界环（容量契约化，2.6GiB/h 增长消除）；读头中心初始化 + 单读头绕点淡化：0st×mix0/50/100×link0/100 恰一脉冲于报告 latency（含 0st）；4 级预滤波 20k+12st 折叠 0.257→0.038；Glide 绝对时间；Link=−st·(1−link) 契约修正（谱矩心方向验证） | 8240e782 |
| H06 | ✅ | 三个死参数全消费（glide/tracking/vocoder_formant_st 合成轴独立缩放）；F0 局部极大+最短强 lag（八度保护）、vocal/wide 范围、参考声道按能量、confidence=检测置信度；unvoiced 按高频占比门控（浊音注入 ~1e-3） | dec388eb |
| H07 | ✅ | Dist 全状态逐声道（静音 R 严格 0）；有状态 2× 过采样 FIFO 群延迟对齐（无块尾空洞，1/255/257/1920/随机块逐位一致）；bitcrush hold 绝对流索引；拓扑 dry/并联直通对齐（mix=0=对齐 dry） | 9763e3ad |
| H08 | ✅ | 子块交错触发/渲染（1s 块丢 16→0）；读龄覆盖容量+粒跨度（Reverse seed19 零丢弃，容量 8s）；feedback=固定 D=size_n 样本延迟环；Cloud/Scatter 契约分化；同 seed 重放一致 | 1b282dfa |
| H09 | ✅ | Freeze 捕获资产：`FreezeCaptureStore` sidecar（.f32+.json 原子写，raw PCM 不进参数 JSON）；freeze_capture_id 元数据引用（never lane）；保存→重开 PCM 一致（float32 资产精度）；双捕获各自重放；注册表接导出/实时 | 131c301d/bfb8773b |
| H10 | ✅ | MiniLimiter 逐采样包络（min-decay log 域向量化）：1s 恢复所有块长=1.0、×4→0.25→0.25·e 精确；拓扑时延对齐随 H07；1 采样块/路由切换/reset+换率+Freeze 不崩溃 | c4729079 |
| H11 | ✅（见下） | 滑条按住停顿不提交、松手单次提交；预设 ±/删除接 effect_pret 持久库（稳定 id 幂等，打开合并显示）；Audition 明确禁用+说明；Mangler 画布真实波形/颗粒分支（演示模拟仅回退） | 2e29edc3 |
| H12 | ✅ | from_dict 非有限值→默认（NaN 增益不再穿透 DSP）；seed 宽容解析；`automation_param_kind`（never/event/continuous）+采样端 never 强制跳过；`validate_effect_params` 统一校验+诊断 | aa16dd64 |
| H13 | ✅（见下） | cProfile 定位：pitch 逐样本 Catmull-Rom 42%、F0 O(n²)。段向量化 gather + FFT 自相关后：两新 FX p50 71.97→~7–12ms、p99<20ms、时钟比 0.460x→>3x（audit/h13/ 前后基准）；p99<40ms 预算守卫测试 | 5adb6424 |

### 最终回归

- 复审 102 文件清单：**1796 passed, 1 skipped**（既有 WAV 资产缺失项，与复审基线相同）。
- 新增 H01–H13 回归 12 文件 **141 项**全绿；state-export 探针全部指标转绿
  （bus/master f20、三路径 clip tail、非零选区==从头裁剪、全路径尾音）。

### 剩余问题诚实清单（未宣称完成）

1. **VC-01 精度**：Doppler 法 ±12st 的音高精度仍受绕点淡化边带影响
   （~±25 cents 量级，原目标 5 cents 需 WSOLA/锁相声码器路线）；绕点窗
   内能量以 ±66Hz 边带扩散（RMS 守恒，谱峰锐度下降）。已按计划要求
   明示为质量限制而非改写验收。
2. **H11 未尽项**：完整 begin/preview/release/cancel 手势 token 化（当前
   为按住冻结+松手单提交）、A/B 与 Copy 契约、clip 级 `(clip_id, effect_id)`
   独立遥测 tap（当前 clip 分析复用轨 tap）、Audition transport 接入
   （按钮已明确禁用）。
3. **H09 未尽项**：Freeze 捕获的工程 JSON 侧 id 清单持久化字段
   （`freeze_capture_ids`）由宿主在工程保存时写入；undo/redo 事件历史
   与资产 GC 的 command 化；Stem 路径的注册表参数传递。
4. **H12 未尽项**：registry schema 的完整范围/单位元数据表（当前
   kind 三类 + DSP np.clip 兜底）；打包后端加载矩阵（目标平台安装包
   实测）。
5. **H13 未尽项**：本机单机基准（不外推）；R10 arm/record/monitor 分离
   与自然退出收尾、真机热拔插；R12 真实多声道图；R13（VCA/True Peak/
   事件自动化）按产品排期。1–16 轨/多 FX/多采样率矩阵报告待做。
6. **全链路图级 PDC**：模块时延已如实报告并 dry 对齐（H05/H07/H10），
   但全局 PDC 补偿器（跨轨相位对齐）未实施。
