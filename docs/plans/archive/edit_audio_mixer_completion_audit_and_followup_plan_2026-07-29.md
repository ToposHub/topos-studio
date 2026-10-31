# 剪辑页 Audio Mixer 完成后复审：残留缺陷与第三轮优化计划（2026-07-29）

## 1. 结论

此前计划中的大部分模块、数据模型、UI 与对应单元测试已经落入代码；但“代码已存在”不等于“可作为专业剪辑混音工作流交付”。本轮以旧计划的验收项反查运行路径后，确认仍有 **4 个 P0 发布阻断项、8 个 P1 工作流/正确性缺口和 2 个 P2 产品化缺口**。

最关键的判断如下：

1. 当前可运行环境没有 `scipy`，EQ、LUFS、响度扫描、响度规范化以及依赖它们的 Bus/导出一致性均不可用；应用虽然有 capability gate，却还不能交付这些能力。
2. `AudioGraphSnapshot` 和 `AudioGraphRenderer` 已实现，但没有接入实时播放；实时 `AudioMixer` 与离线导出仍是两条实现路径。它们已出现可证明的处理差异：实时路径没有剪辑速度和剪辑淡化。
3. Bus/solo 图的闭包语义、Bus 自动化、Bus Send 和非法路由处理不完整，复杂工程可能无声、错误落到 Master，或与用户选择的 Solo 不一致。
4. R 按钮已经能触发录音，但停止时会重复导入 take，且 `EditProject.import_asset()` 的返回新项目没有回写；录音实际上不能可靠进入工程。所谓 BWF 也只是 WAV `INFO` 标签，不含 `bext`/时间参考。

因此，旧计划可以标记为“功能实现完成”，**不能标记为“端到端验收完成”**。本文件仅记录审计与后续计划，不修改运行代码。

## 2. 审计范围、方法与当前基线

审阅范围：

- 实时链路：`src/features/edit/ui/edit_page.py`、`src/features/edit/core/audio_playback.py`
- 离线/交付链路：`audio_graph_renderer.py`、`timeline_export.py`、`stem_export.py`
- 图与路由：`audio_graph_snapshot.py`、`channel_layout.py`、Mixer routing UI/Controller
- 录音与互操作：`recording_manager.py`、`project.py`、`interchange.py`
- 功能开关和相关 pytest 回归集。

本次执行：

```bash
pytest -q \
  tests/edit/test_audio_capabilities.py \
  tests/edit/test_audio_dsp.py \
  tests/edit/test_loudness_meter.py \
  tests/edit/test_audio_graph_snapshot.py \
  tests/edit/test_audio_graph_parity.py \
  tests/edit/test_audio_export_parity.py \
  tests/edit/test_audio_signature_completeness.py \
  tests/edit/test_audio_bus_registration.py \
  tests/edit/test_audio_bus_routing.py \
  tests/edit/test_audio_automation.py \
  tests/edit/test_clip_audio_fade.py \
  tests/edit/test_loudness_analyzer.py \
  tests/edit/test_loudness_delivery.py \
  tests/edit/test_mixer_placeholder_entrances_disabled.py \
  tests/edit/test_mixer_preset_completeness.py \
  tests/edit/test_audio_playback.py \
  tests/shared/test_audio_mixer_units.py
```

结果：**497 collected，463 passed，34 failed**。运行解释器是 Python 3.13.5，`python3 -m pip show scipy` 显示未安装。34 个失败集中在 EQ、LUFS、响度分析/交付、Bus EQ 与 EQ 导出一致性；这证明目前的 capability gate 只是安全降级，尚不是可用的发行配置。

下表中的“证据”是当前代码路径或这次测试的可复现事实；“验证”是完成该项时必须新增或调整的验收，不以“方法存在”替代。

## 3. 已确认问题与可落实的修复方案

| ID | 优先级 | 问题、影响与代码证据 | 具体优化方法 | 完成验证 |
|---|---|---|---|---|
| RMA-01 | P0 | **DSP 运行依赖没有被实际交付。** `requirements.txt`/`pyproject.toml` 声明了 SciPy，但当前测试环境未安装；`AudioCapabilities` 因而判定 DSP 不可用，EQ 与 LUFS 静默旁路或返回 -70。上述 34 个失败均由此链条触发。 | 1) 固化受支持 Python/平台的锁定依赖或安装脚本；2) CI 首先在全新虚拟环境执行安装，再跑音频回归；3) 保留 capability gate，但当用户启用 EQ/LUFS/规范化时在 UI 显示阻断状态、原因和修复动作；4) 为没有 SciPy 的构建明确选择“禁用并不可导出”或等价 DSP fallback，不能伪造测量结果。 | CI 的 clean-install job 在目标 OS/Python 全绿；`test_audio_capabilities` 的 backend-present 分支通过；无后端时 UI/导出返回结构化错误，且没有把 peak 标为 LUFS、没有静默产出未处理文件。 |
| RMA-02 | P0 | **唯一音频图尚未落地。** `audio_graph_snapshot.py` 自己说明实时仍使用 `AudioMixer.mix_samples`；全仓没有播放线程消费 `SnapshotHistory`/`build_audio_graph_snapshot` 的调用。`audio_graph_renderer.py` 也明确只给离线导出使用。UI 改动仍通过签名重建/直接 setter 进入实时 mixer。 | 定义 `CompiledAudioGraph`：由不可变 snapshot 编译 decoder、layout、DSP、routing 与自动化运行态。播放线程只在 block 边界接收已验证的新 graph；离线导出、stem、响度分析由同一节点处理器按块驱动。删除“两个实现保持语义相同”的长期承诺，只保留一份实现和测试 oracle。 | 对每种 graph 变更（轨道/clip/Bus/FX/automation/route/master）记录 revision；断言播放线程只在块边界切换；用固定 PCM fixture 比较 realtime、offline、stem 三者，误差阈值按 DSP 类型定义（纯 gain/route 为 1 LSB，滤波器为稳定容差）。 |
| RMA-03 | P0 | **实时剪辑速度和剪辑淡化未生效。** `EditPage._sync_audio_tracks_from_timeline()` 调用 `AudioPlaybackManager.add_track()` 时只传 source start、fps、timeline range，不传 `clip.speed`、fade in/out；`AudioPlaybackManager.add_track()`/`AudioDecoder` 也没有这些参数。导出有 speed/fade 处理，故两条路径必然漂移。 | 在 `ClipNode` 明确 `source_start`、timeline interval、speed、reverse、time-stretch policy、fade curve；实时和离线使用同一个 clip reader。先定义产品策略：仅 resample（变调）或高质量 preserve-pitch；不要把按视频帧 `int()` 跳源帧当作 time-stretch。以样本位置而非视频帧取整。 | 0.5x、1x、2x、反向（若支持）的 impulse/1 kHz fixture 均验证时长、源位置和频率策略；线性/S 曲线 fade 包络误差 <0.1 dB；同一 fixture 的实时 ring-buffer PCM 与离线 PCM 一致。 |
| RMA-04 | P0 | **Solo 的可听图闭包错误。** `mix_samples()` 先剔除所有未 solo 的普通轨，随后又剔除所有未 solo 的 Bus：solo 一个普通轨时其必经 Bus 会被跳过；solo 一个 Bus 时其上游普通轨被跳过。`AudioGraphRenderer.render_bus_stage()` 有相同逻辑。 | 编译图时计算 `audible_nodes`：solo 普通轨保留该轨、其所有输出祖先 Bus 及必要 effect return；solo Bus 保留其所有上游输入和下游到 Master 的链路。支持明确的 `solo_safe`/`solo_isolate` 策略，避免发送效果意外消失。将这个集合作为实时、导出、stem 的唯一判断。 | 建立四组 PCM oracle：solo source、solo Bus、多 solo、solo source + send/return；每组分别跑实时/导出。断言选中的信号存在、未选闭包外信号不存在，且静音优先级高于 solo。 |
| RMA-05 | P1 | **非法路由会静默改写声音。** `validate_routing_graph()` 只被 UI 预览与未接入的 snapshot 使用；实时检测到 cycle 后仅日志并跳过 cycle，未知 `output_bus_id` 在实时/离线都回退到 Master。导入旧工程、命令、脚本或撤销路径都可绕过 UI 校验。 | 在所有图激活入口（项目加载、命令提交、undo/redo、播放同步、导出）运行同一编译校验。无效图不得激活：保留上一个有效 graph，并把准确错误显示到 Mixer/Export；导出应失败并报告，而不是擅自发往 Master。为工程迁移提供“修复路由”向导。 | 参数化构造 self-cycle、multi-cycle、未知 output、未知 send、删除 Bus 后残留 route；断言实时不换图、导出不写文件、UI 有可定位错误；修复后才恢复可播放/可导出。 |
| RMA-06 | P1 | **Bus Send 和 Bus 自动化没有进入处理。** 普通轨阶段处理 sends，但 Bus 阶段只处理 `output_bus_id`；`bus_state.volume/pan` 和 renderer 的 Bus node 也从不读取 automation。UI/模型可保存这些字段，结果却无声忽略。 | 统一把 normal track 与 Bus 看作可路由 node：每个 node 都可有 output、pre/post send、automation、meter tap。拓扑排序要纳入会产生反馈的 send（或明确规定 send 只能指向 return Bus 并做延迟断环）；在模型/UI 中显示 send 的 pre/post 位置。 | 覆盖 Bus→send→FX return、Bus volume/pan automation、Bus mute automation、非法反馈；逐块与离线结果一致。对 automation keyframe 前/中/后采样并验证量值与时间位置。 |
| RMA-07 | P1 | **Mute 自动化不是样本精度，离线 master mute 甚至只读取导出起始帧。** 实时 track/master mute 只在当前 block 起点判断；`AudioGraphRenderer.apply_master()` 对整段 output 只读 `start_frame`。导出中途的 mute keyframe 会被整体忽略，阶跃还可能产生 click。 | 为离散参数定义规则：mute 关键帧在精确 sample 边界切换，并可选 1–5 ms de-click ramp；自动化 evaluator 输出 bool/float block timeline，而不是单一 frame 值。导出按 block 渲染，不能对整段 PCM 只评估一次。 | 10 秒素材在第 3 秒 mute、第 6 秒解除；验证 3–6 秒为静音、边界无超阈值 click、实时/离线一致。轨道、Bus、Master 各一组。 |
| RMA-08 | P1 | **声像语义没有区分 mono pan 和 stereo balance。** 当前对任何多声道输入都将 L/R 乘 equal-power gain；中心位置为每侧 -3 dB，原生立体声会整体降低且改变声像。既有测试也把这种行为写成预期，说明问题被测试固化。 | API/UI 明确模式：mono 使用 equal-power pan；原生 stereo 默认使用 unity-center balance（只衰减远侧）或可选 dual-pan；5.1/7.1 不允许套用 stereo pan，改用 layout-aware trim/panner。迁移旧项目时保留旧模式或给出一次性兼容选项。 | 同幅 stereo 1 kHz 在 center 的每通道应保持 0 dB（balance 模式）；mono center 总功率保持恒定；pan sweep 的能量变化满足定义。实时、导出、Bus 三处共用同一 panner fixture。 |
| RMA-09 | P1 | **Channel layout 仍按声道数猜测并在失败时截断。** `adapt_channels()` 默认由 count 推断，实时/导出没有把媒体容器实际 layout 传入；异常时又回退截断/复制，与模块注释“拒绝未知 mapping”相反。`5.1(side)`、`5.1(back)`、multiple mono 等会被错误解释或丢声道。 | 导入时保存 PyAV/容器 layout、channel order、language/role；`AudioDecoder` 输出携带 layout，编译 graph 选择显式映射。未知/不支持 layout 必须要求用户映射或拒绝导出；把 LFE inclusion、downmix preset、fold-down 目标写进交付设置并随工程保存。 | 为 mono/stereo/5.1(side)/5.1(back)/7.1/未知 layout 构造逐声道 impulse；断言目标通道和系数。未知布局不能静默产生文件；保存-重开-导出后 layout metadata 与 PCM mapping 不变。 |
| RMA-10 | P1 | **长片导出和长录音存在无界内存/二次复制风险。** `timeline_export.py` 先分配整个 `output`，每个 Bus 又可能分配全时长 `bus_inputs`；一小时 48 kHz stereo float32 的 master 约 1.3 GB，Bus 数量线性叠加。`RecordingManager` 每个 `readyRead` 使用 `np.concatenate`，录音越长复制越多，且所有 PCM 最后才落盘。 | 做 bounded-memory streaming graph：以固定 audio block（如 1024/2048 samples）解码、混音、保持 DSP 状态、写 encoder；Bus 仅保留当前 block。录音改成临时 WAV/RF64 流式 writer 或固定 chunk 队列后台写盘，最终原子 rename；增加背压、磁盘剩余空间和丢帧 telemetry。 | 1 小时/16 轨/8 Bus 合成 fixture：RSS 上限按 block 数而非时长增长，导出成功且音频连续；录音压力测试中 buffer 不持续增长、无 O(n²) 延迟，磁盘满/设备断开保留可恢复 partial take。 |
| RMA-11 | P0/P1 | **录音导入、丢弃与 BWF 声明不可靠。** P0：`_start_recording()` 注册 `on_take_completed`，而 `_stop_recording_and_import()` 在 `stop_recording()` 后又显式调用 `_on_take_recorded()`，同一 take 会导入两次；且 `EditProject.import_asset()` 返回新的不可变项目，但 controller 将 `new_project` 丢弃，take 不会可靠回写到当前项目。P1：`discard_current_take()` 只改 status，随后 finalize 会覆盖为 completed 并写文件。P1：`write_take_wav()` 写的是 `ICMT/IART/INAM` INFO tag，没有 BWF `bext` chunk、TimeReference 或可用 timecode。 | 1) 规定 take 完成只有一个事件源：callback 或返回值二选一；2) 用项目命令/状态管理器接纳 `import_asset()` 返回的新项目并刷新 bin、保存脏状态，保证幂等；3) discard 应停止/清缓冲并删除临时文件或转入明确的 discarded 状态；4) 实现真正 bext（originator、originator reference、date/time、TimeReference、coding history），无法实现则 UI/互操作评估必须标为“WAV with INFO metadata”，不可称 BWF。 | controller 级端到端测试：一次 R 开始/停止只产生一个 asset、一个 bin 条目、一次 undo；重开工程仍存在。discard 后无 completed 文件/asset。用独立 BWF parser 检查 bext 与 sample time reference；无 bext 的文件不通过 BWF capability test。 |
| RMA-12 | P2 | **功能状态和诊断仍有产品化遗漏。** `Purge Cache` 明确为 false，controller 槽仍只有日志；录音 flag 的文件头仍保留“占位”历史描述，容易误导维护者。DSP、设备、routing graph、underrun、缓存/临时文件没有统一的可见健康面板。 | 删除不可用入口或维持禁用并提供原因；建立 `AudioHealthReport`（DSP backend、设备、sample rate/layout、graph revision/validation、underrun、peak/LUFS、disk/cache）；在导出前做 preflight，报告可复制进 issue。 | 在无 SciPy、无输入设备、非法 route、磁盘不足、缓存待清理五种状态分别截图/自动 UI 测试；用户能在一次点击内得到原因和安全动作，不能看到“可用但实际无效”的控制。 |
| RMA-13 | P2 | **回归测试偏向实现存在性，缺少真实链路和失败路径。** 例如 record-arm 测试直接测 `RecordingManager`，没有验证 controller 的重复 callback/不可变 project 回写；snapshot 测试不验证播放线程采用 snapshot；现有 parity fixture 未覆盖 clip speed/fade、Bus solo closure、Bus sends/automation、非法 route。 | 以“用户可听/可交付”编排测试层级：pure DSP → compiled graph PCM oracle → manager/controller → headless export → 有设备的 nightly smoke。为每个 P0 写一个先失败后修复的回归 test；把旧的错误预期（如 stereo center -3 dB）改为产品语义测试。 | CI 分层：fast unit、clean-install DSP、headless integration、nightly 真实设备/长时压力。每个 RMA ID 在测试名或 marker 中可追溯；主干禁止在 P0 oracle 失败时合并。 |
| RMA-14 | P2 | **交付互操作仍需诚实定义边界。** 当前 BWF 能力被评估为可用，但实际仅 INFO metadata；AAF/OMF/XML 若仍是“评估/不可用”状态，应在导出菜单、文档和 capability API 中一致表达，避免给影视后期流程错误预期。 | 建立格式能力矩阵：每个格式列出读/写、timecode、relink、track role、automation、plugin、已验证的软件版本；先把不可用格式隐藏或标为预览。优先完成 WAV/BWF、stems、CSV cue 的完整验证，再根据实际用户需求选 AAF 或 OMF 之一实现。 | 对每种标称可写格式使用独立 parser/目标软件导入 smoke test；能力矩阵和 UI 状态自动由测试结果生成，未通过的能力不可显示为“已支持”。 |

## 4. 第三轮实施计划表

### 阶段 A：恢复可发布性与避免错误交付（第 1 周，P0）

| 序号 | 工作项 | 实现步骤 | 验收门槛 | 负责人产物 |
|---|---|---|---|---|
| A1 | 固化 DSP 环境 | 建立锁定依赖/环境 bootstrap；CI clean install；启动/导出 preflight 接 `AudioCapabilities`。 | SciPy backend present 回归全绿；缺后端时 EQ/LUFS/normalize 不能静默继续。 | lock/bootstrap、CI job、健康状态 UI、RMA-01 测试。 |
| A2 | 修复录音接入事务 | 去掉重复完成回调；用项目 command/状态容器接收新 `EditProject`；导入幂等与 undo；修复 discard。 | 一次 take 恰好一个 asset；保存重开可见；discard 不落文件。 | controller/项目命令、端到端测试（RMA-11）。 |
| A3 | 修复 solo audible closure | 抽出 graph-level closure 计算，接入 realtime/export/stem。 | 四组 solo PCM oracle 全绿，track/BUS solo 都有声且只含期望路径。 | closure compiler、RMA-04 测试。 |
| A4 | 修复 clip 的实时一致性 | 扩展 clip runtime 参数，先实现准确 fade 与明确的 resample speed；preserve-pitch 若未实现必须 UI 限制。 | speed/fade realtime/offline fixture 一致。 | clip reader、RMA-03 测试、兼容说明。 |

**阶段退出条件：** clean-install CI 通过；没有 P0 已知的“预览与导出不同/录音丢失或重复”路径；发布 preflight 能阻止缺 DSP 的误交付。

### 阶段 B：把两条混音链收敛为可编译音频图（第 2–3 周，P0/P1）

| 序号 | 工作项 | 实现步骤 | 验收门槛 | 负责人产物 |
|---|---|---|---|---|
| B1 | 接入 immutable snapshot | 在 timeline mutation 后生成 snapshot；graph compiler 做完整校验；播放线程 block-boundary 原子换 graph。 | 日志可追溯 revision；错误 graph 保持上一有效 graph。 | `CompiledAudioGraph`、swap 生命周期、RMA-02/05 测试。 |
| B2 | 统一 block renderer | 把 clip、track、Bus、master 的处理抽到一份 stateful block renderer；实时 ring、导出 encoder、stem/analysis 都调用它。 | 纯路由/gain 1 LSB，DSP 通过稳定容差的 PCM parity。 | 单一 renderer、parity oracle。 |
| B3 | 完整路由语义 | 校验和激活门禁；Bus Send、pre/post send、Bus automation、feedback policy。 | 无效图无声失败而非回退 Master；Bus send/automation parity 通过。 | routing compiler、错误面板、RMA-05/06 测试。 |
| B4 | 自动化边界 | 给 volume/pan/mute 制定 sample/block 精度与 de-click 策略；修复 master 导出只读起始帧。 | 中途 mute、ramp、Bus/Master 曲线在实时/离线准确且无 click。 | automation evaluator、RMA-07 测试。 |

**阶段退出条件：** 不再存在“实时 AudioMixer 实现”和“离线语义复制实现”；任何项目状态先通过 graph compiler 才能播放或导出。

### 阶段 C：专业声道、声像与大工程性能（第 4–5 周，P1）

| 序号 | 工作项 | 实现步骤 | 验收门槛 | 负责人产物 |
|---|---|---|---|---|
| C1 | layout truth chain | asset probe → decoder frame → graph node → export metadata 全程带 layout；提供明确 mapping UI。 | 逐声道 impulse、重开工程、导出 metadata 三重验证。 | layout model/migration、RMA-09 测试。 |
| C2 | 重定声像语义 | 区分 mono pan、stereo balance/dual-pan 与 surround trim；迁移旧工程。 | center unity/恒功率要求分别满足；旧工程可选择兼容行为。 | panner API/UI、RMA-08 测试。 |
| C3 | 流式渲染/录音 | 输出、Bus、响度扫描改为固定 block；录音后台流式写临时文件并具备恢复。 | 1 小时压力测试 RSS 有上界，声音连续，故障有 partial take。 | streaming renderer/writer、telemetry、RMA-10 测试。 |

### 阶段 D：影视录音、互操作与产品健康（第 6 周，P1/P2）

| 序号 | 工作项 | 实现步骤 | 验收门槛 | 负责人产物 |
|---|---|---|---|---|
| D1 | BWF/ADR 完整性 | 真 bext/TimeReference、take sheet、pre/post roll、延迟补偿持久化；设备选择与输入电平。 | 独立 parser + 目标软件导入成功；take 与 timeline 时间点可对齐。 | BWF writer、ADR UI、RMA-11/14 测试。 |
| D2 | 健康与 preflight | 汇总 DSP、设备、graph、磁盘、cache、underrun；禁用 Purge Cache 或完成实际清理。 | 五类失败均能解释、阻止或安全降级；报告可导出。 | `AudioHealthReport`、preflight UI、RMA-12 测试。 |
| D3 | 回归体系 | 把所有 RMA oracle 加入 CI 分层；长时/设备测试独立 nightly。 | 每个能力声明可追溯到自动验收；P0 失败阻断合并。 | test markers、CI dashboard、能力矩阵。 |

## 5. 验收总表与发布判定

| 发布级别 | 必须满足 | 当前状态 |
|---|---|---|
| 可用 Beta | A 阶段全部完成；DSP clean install 全绿；实时/离线的 speed、fade、solo、录音 import 不再漂移。 | **未满足**：RMA-01/03/04/11。 |
| 剪辑混音可交付 | B 阶段全部完成；非法 route 不会改写声音；track/Bus/master 自动化有 oracle。 | **未满足**：RMA-02/05/06/07。 |
| 影视后期专业交付 | C/D 阶段全部完成；layout/BWF/长时性能/健康检查得到独立工具与真实设备验证。 | **未满足**：RMA-08–14。 |

建议顺序不可颠倒：先修正会造成静默旁路、错误声音、重复/丢失 take 的 P0，再把图收敛；在 graph 和输出流式化之前，不继续堆叠新的 FX、修复工具或互操作格式。

## 6. 建议的每次合并前命令

```bash
# 先在全新环境安装项目依赖；不得复用已装有科学计算包的开发环境。
python3 -m pip install -r requirements.txt

# P0/P1 音频核心回归。
pytest -q \
  tests/edit/test_audio_capabilities.py \
  tests/edit/test_audio_graph_snapshot.py \
  tests/edit/test_audio_graph_parity.py \
  tests/edit/test_audio_export_parity.py \
  tests/edit/test_audio_bus_routing.py \
  tests/edit/test_audio_automation.py \
  tests/edit/test_clip_audio_fade.py \
  tests/edit/test_recording_manager.py \
  tests/edit/test_record_arm_wiring.py \
  tests/edit/test_loudness_analyzer.py \
  tests/edit/test_loudness_delivery.py
```

新增 RMA-03～RMA-11 的端到端 PCM/Controller 用例后，再将它们加入同一命令；任何 P0 用例失败都不应以 capability fallback 或 `xfail` 作为发布豁免。
