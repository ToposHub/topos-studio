# Clip Editor 与混音工作区实施计划

## 当前状态与实施顺序

Studio 的 Edit 与 Mixer 已共享时间线、Viewport 和 Audio Mixer。Mixer 使用音频专用轨道头，保留录音按钮和推子、电平表、插入效果、Master / Bus 输出。Clip Editor 可与 Audio Timeline 合并。双击音频片段和切入已选片段的标签使用同一入口。片段增益、帧级淡入淡出、已有采样级淡化、淡化曲线都通过时间线命令提交。当前波形和峰值来自源包络，后续以真实 PCM 增补试听和处理后的测量。

| 优先级 | 任务 | 依赖 | 完成标志 |
| --- | --- | --- | --- |
| P0 | 混音页 Media Pool、Effects | 现有共享面板和注册表 | 能导入、拖放素材，能明确把效果加到片段或轨道，切页仍是同一工程状态 |
| P0 | Clip Editor PCM 取样、试听、频谱、分析 | 统一源解析和采样时钟 | 可隔离试听片段、定位和循环，波形、频谱、听到的声音使用同一 PCM 和坐标 |
| P0 | 可持久化处理栈与渲染路由 | PCM 解析、工程命令栈 | 处理栈可旁路、删除、撤销和重开，试听、两页播放和导出一致 |
| P1 | Audio Timeline 包络线与采样级编辑 | 现有自动化及淡化命令 | 曲线可编辑、可听、可导出，节点拖动一次撤销，帧域与采样域一致 |
| P1 | Clip Editor 区间编辑、声道、标记、归一化 | PCM 和处理栈 | 选区内定向处理与记录可重现，源素材保持原样，正反向试听准确 |
| P1 | 采集噪声与降噪、响度匹配 | PCM、频谱、响度分析和处理栈 | 噪声底下降，原有信号保真，在 True Peak 上限内应用可复现响度增益 |
| P2 | 频谱修复笔、瞬态修复、变速和音高 | 可编辑频谱、处理栈、输出长度事务 | 局部修复有边界、防爆音，时长和音高分别受控，失败或取消无误提交 |
| P2 | 混音台扩展、录音监听、响度与 Stem、布局及性能 | 现有生产音频图 | 总线、发送和自动化可回放和导出，监听独立，长序列和中断场景可恢复 |

## 1. 在 Mixer 增加 Media Pool 与 Effects

1. 在 `DockLayoutMixin._create_audio_workspace_docks` 为 `Media Pool` 和 `Effects` 分配稳定、独立的 Mixer dock objectName。参照 `_attach_edit_panels_to_mixer` / `_restore_edit_panels_from_mixer` 搬运现有面板实例。接入 `_audio_workspace_docks`、`all_docks`、Workspace 菜单和 Mixer 布局保存。旧 `saveState` 用一次性版本迁移补齐新 dock，保留既有的浮动、合并、尺寸和可见性偏好。默认可把两者放在 Viewport 一侧的同一标签组，时间线占底部整宽。无对应功能模块的产品跳过这两个 dock。 [Dock 布局](../../src/app/controllers/dock_layout_controller.py) · [页面切换](../../src/app/controllers/page_controller.py)
2. Media Pool 复用 `MediaPoolPanel` 的工程关联、文件夹、导入、搜索、离线重连、MIME、右键和波形。Mixer 默认突出音频，可临时查看含音频流的视频素材与其他资源。过滤是视图状态，切回 Edit 恢复原过滤和展开状态。检查 TimelineView 的音频模式下媒体拖入命令，对纯音频直接落到目标音轨，对含音频流的视频提供仅插入音频的落点语义。拖到 Bus、锁轨、无音频流媒体时给出定位反馈。避免拖放过程悄悄创建隐藏的视频轨或不期望的链接。 [媒体池](../../src/features/edit/ui/media_pool_panel.py) · [时间线 MIME 接收](../../src/features/edit/ui/timeline_view.py)
3. Effects 复用 `EditEffectsPanel` 的 Audio 标签、分类、搜索、收藏和音频转场。用 workspace scope 切换默认标签及可见分类，回 Edit 恢复用户设置。把效果拖到片段上时复用现有 Clip FX 命令。拖到 Audio Mixer 插槽或调用明确的“添加到当前轨道”动作时，复用 `StudioAudioMixerPanel` 的 Track/Bus/Master 插入链，校验注册表声明的目标和 DSP 能力。双击默认使用可见的主选区目标，目标不明确时提示选择片段或插槽。效果面板与插槽共同用 `effect_id` 和既有 MIME；分类、参数和默认值从 `EffectCatalog` 读取。对不支持的作用域或预览/导出能力显示理由。 [Effects 面板](../../src/features/edit/ui/edit_effects_panel.py) · [效果目录](../../src/features/shared/effects/effect_catalog.py) · [Audio Mixer](../../src/features/mixer/ui/studio_console.py)
4. 实测先从 Edit 导入 WAV、MP3 和带音频的 MOV，切 Mixer 后在同一文件夹拖入工作序列。拖放 Clip FX、Track FX 和音频转场后分别核对三个效果目标，切 Edit 查看并撤销，逐项导出。这一阶段补 `tests/mixer/test_project_workspace.py` 的跨页借还、MIME 和两次切页不重复连接信号测试，含锁轨、Bus、丢失素材及旧布局恢复。

## 2. PCM、频谱和独立试听

1. 新建纯数据 `ClipAudioRenderSpec`，由 `(project_id, timeline_id, clip_id, revision)` 读取稳定模型并解析为采样率、声道布局、源指纹、源范围、绑定音频、映射和处理栈。复用 `resolve_audio_source` / `bound_audio_frame` 及 `AudioDecoder` 已有的源帧、音频绑定、倒放和 retime 语义，时间轴用 `TimelineSampleClock`。测试 23.976、29.97、非零 `source_start` 和 `source_sample_offset`、外录 WAV 正负偏移、视频内嵌音轨、重连、倒放、混合帧率。实测脉冲的播放、频谱标记、导出样本位置，容差 1 sample。冻结或后端明确不支持的映射应显示阻断原因。 [源解析](../../src/features/edit/core/audio_sync.py) · [采样时钟](../../src/features/edit/core/sample_time.py) · [现有解码](../../src/features/edit/core/audio_playback.py)
2. 原始输入为平面 `float32[C, N]` 并明确 `channel_layout`。解码和重采样放在可取消的 worker。用原子发布、有上限的 PCM/可视化缓存，区分只读源范围与已提交的处理版本。多分辨率波形计算 min/max/RMS，选区边界测量直接从有效 PCM 取样。Hann STFT 输出每个时间块 `[频率 bins]`，记录 dB 范围和 Nyquist；屏幕上的对数频率缩放由 canvas 映射。结果返回前校验来源、处理栈、选区和请求 generation。切片段、重连、切换工程、撤销后旧结果不会覆盖新窗口。优先生成眼前视区，拖动或缩放时做节流。 [当前波形提取器](../../src/features/edit/core/audio_waveform.py) · [UI 数据入口](../../src/features/mixer/effects/panels/clip_editor.py)
3. 复用 `TakeAuditionPlayer` 的 Qt 音频设备和生命周期思路。新增 worker 填充缓冲、由 `QAudioSink` 或现有 scrub/试听服务消费的会话。打开 Clip Editor 保持当前时间线播放策略；按下试听时暂停时间线和其他试听，按设备已消费帧回填 `update_playhead` 和原音频通道电平。播放键、停止、波形 seek、循环、跳过选区、原声对比都指向一个会话。循环无选区时用整段，有选区时用半开区间 `[start, end)`；跳过试听把游标从 start 跳到 end，可加短交叉淡化。停止回到开始试听的游标，离开标签、关窗、工程切换和设备失效时停止并释放。A/B 切换保留共同时间坐标，长度变化时按映射找同一源位置；添加用于音量匹配的监听选项时保持工程增益不变。 [ADR 试听](../../src/features/edit/ui/adr_take_sheet_panel.py) · [Clip Editor 生产适配层](../../src/features/mixer/ui/project_clip_editor.py)
4. 过零吸附以编辑工作采样率在候选时刻附近搜最近符号变化，默认容差 3 ms 且受时间选区和素材边界约束。单声道使用对应声道。立体声按候选样本两声道绝对幅值和到原游标的距离打分，找不到足够小的跳变时保留落点并用短淡化。避免立体声反极性时用 L+R 的零值误判。保存样本索引，缩放改变时不重写。测试 DC 偏置、立体声反极性、无过零、等距、屏幕缩放、单样本边界。

## 3. 非破坏性处理栈与工程提交

### 数据、坐标和渲染合同

- 给 `ClipInstance` 增加版本化结构，例如 `audio_edit = {version, processing_rate, steps, markers, envelope}`。每个 step 保存稳定 `step_id`、`processor_id`、`algorithm_version`、参数、启用状态、输入采样区间和目标声道。区间是半开整数样本，持久化前逐项检查长度、坐标空间、单位和声道 ID。对改变输出长度的 step 同时记录输入/输出映射，后续 step 的坐标域属于上一步输出。旁路或移除导致下游落点不可重映射时，显示冲突并允许取消或重定位。新 schema 支持迁移和未知字段 round-trip，未知 processor 阻断应用和交付并给出逐条诊断。 [片段模型](../../src/features/edit/core/timeline_v2.py) · [工程 schema](../../src/shared/project/schema_v1_spec.py)
- 播放顺序固定为源解码、裁剪/绑定和基准 retime → Clip Editor 栈 → native clip 增益、包络和淡化 → Clip FX/转场 → Track FX、音量、Send/Bus → Master。合并到现有音频图时要保留原有 Clip FX、淡化和转场的相对顺序。若现有消费者对某段顺序有分歧，先以无处理栈的 baseline 做 null test，再在共用边界加入 processor。公开消费者共用一份 source/render plan，逐一覆盖 Edit 的实时同步、scrub、Deliver 离线混音和 Stem。把 `step`、预渲染结果及其版本加入 snapshot/hash，可避免改变处理栈后错误重用音频图。 [实时同步入口](../../src/features/edit/ui/edit_page.py) · [音频图快照](../../src/features/edit/core/audio_graph_snapshot.py) · [音频图](../../src/features/edit/core/audio_graph_renderer.py) · [导出入口](../../src/features/edit/core/timeline_export.py)
- 第一版把整段和 STFT 工具统一做成可重建的预渲染 PCM，后台按块产出，下游共用同一版本。按规格哈希索引工作采样率、源指纹、绑定、输入范围和有序 steps。可重建缓存放在缓存目录，工程保存结构化参数与资源 ID；显式“渲染成独立素材”才通过 `ResourceIndex` 登记项目生成音频，包内使用稳定资源 URI。使用临时文件加校验后原子替换，任务中途取消能清掉临时文件。回收缓存前保护正在播放、导出或被 undo/redo 使用的缓存；缓存缺失自动重建并暴露进度。不覆盖源素材，也不会替换链接视频的资产。 [资源解析](../../src/shared/project/resource_resolver.py) · [包资源规范](../../src/shared/project/package_spec.py)
- 每次 Apply 先构建候选 spec 并后台渲染，结果 ready 且输入版本仍匹配时才提交。按钮连点去重，撤销、重做、旁路、移除、清空均走同一时间线历史。改变时长先展示是否裁短留空或执行 ripple，检查链接 A/V、多轨同步锁、相邻片段、转场、工程标记和序列边界。改时长或添加项目资源时用 `TimelineProjectTransactionCommand` 一次提交项目与时间线。无长度变化的操作保持片段 ID、序列区间和链接关系。 [时间线/项目事务](../../src/features/edit/core/timeline_project_transaction.py)

### 控件到命令的映射

| UI | 实施方法 | 验收 |
| --- | --- | --- |
| Apply gain、L+R / L / R、静音选区 | 保留现有整段增益；选区/单声道生成为 `range_gain`。dB 转线性 `10 ** (db / 20)`。静音使用零增益，和原声做 2–5 ms 边缘淡化。两声道内部在源布局中处理后再进入轨道混音。 | 合成左右频率不同的 stereo WAV，只改左声道样本且保持右声道；选区外信号逐样本不变；撤销还原。 |
| 裁剪、删除区间 | 裁剪保留明确选区；内部删除把两侧拼起来并短交叉淡化。若落点可表示为 native trim，则复用时间线命令；否则渲染为预览草稿并显示长度变化。空选区、整段删除、相邻片段冲突单独验证。 | 对有链接视频、带子帧偏移、接转场的片段分别验证默认影响范围、冲突提示、一次撤销和重开。 |
| 标准化 | 从目标样本区间计算真实 sample peak，整段或分声道应用增益。零信号标记“无可归一化信号”；用响度工具处理 True Peak 上限。 | `−12 dBFS` 合成正弦标准化到 `−3 dBFS`，误差 ≤0.1 dB；全零不生成无意义 step；区间外不被处理。 |
| 声道独奏、静音、相位 | 独奏作为 Clip Editor 临时监听。工程静音、反极性作为独立 channel mask。反极性对选定通道乘 `−1`，交换 L/R 另用通道映射。 | 单声道反极性和原信号相加为零；做两次相消；用互异声道测试 L/R 内容未交换。 |
| 标记点 | 将 `markersChanged` 映射成增删改命令。保存稳定 ID、局部样本、名称、颜色，明确区分片段标记与序列标记。移动片段时标记随片段，裁切隐藏移出边界的标记，改变长度按 spec 映射。 | 移动、裁切、倒放和保存重开后核对同一源位置；复制片段重新分配 marker ID。 |
| 归零和 20 ms 防爆音 | 扩展现有帧域/样本域淡化接线。已有 sample fade 走 `UpdateClipSampleFieldsCommand`；普通片段启用 20 ms 前，把时间线图形拖动、快照、裂片、undo 和导出统一到优先生效的 sample fields。用同一时钟和同一曲线应用两端，极短片段下两端总长度不得超片段。 | 24 fps 和 29.97 fps 下 20 ms 都精确为 960 个 48 kHz 参考样本。用平波音频在时间线、Clip Editor 和导出比对曲线；再拖动 Timeline 淡化点，确认渲染有变化。 |
| Process Stack、A/B、Undo/Redo | 由工程模型驱动列表和旁路按钮。生产适配层使用现有 signals，把原型的本地 `_push_process_step` 和模拟数据变更从生产调用链移除。排队、计算、失败、已提交状态可见。原音按同一源范围生成，A/B 是临时试听。 | 连续改两步顺序、撤销重做、取消任务、失败重试、工程离开重开。原文件摘要一致，Clip Editor 与同一片段导出做校验。 |

## 4. Audio Timeline 包络线

1. 分开实现**片段音量包络**与**轨道自动化**。片段包络随 clip 移动，按局部样本存点，放在波形上。轨道/Bus/Master 自动化按序列帧存点，显示为独立 lane，复用 `EditTrack.automation`、`AutomationRecorder` 和 `AddAutomationKeyframeCommand` 等命令。第一阶段做 Clip Gain 与 Track Volume 两种模式，再接 Pan、Send、FX 参数。 [轨道自动化命令](../../src/features/edit/core/commands/audio_commands.py) · [自动化录制](../../src/features/edit/core/automation_recorder.py)
2. Clip Gain 通过 Clip Editor 的 `envelopeChanged` 和 Timeline 上的同一 command/service 修改同一组节点。节点结构 `{id, local_sample, gain_db, interpolation}`，同 sample 唯一，首尾默认保持邻近值。用 0 dB 细线，选中时显示 dB 和局部时间。Alt/Option 单击添加节点，拖动时 Shift 锁定一轴，Delete 删除所选节点，仅在包络编辑获得焦点时拦截。普通 Selection Tool 保持现有片段拖动、剪切、淡化和吸附。拖动过程中用本地预览和节流参数快照，松手提交一条命令，Esc 恢复。多选按组平移时限制顺序与边界。轨道头提供开关、当前参数和 Read/Touch 状态。大时间线按可见片段和像素密度裁剪绘制。 [时间线视图](../../src/features/edit/ui/timeline_view.py) · [片段图元](../../src/features/edit/ui/timeline_items.py)
3. 共用 block evaluator，按绝对局部样本对 dB 做分段线性插值，再转线性振幅 `env = np.power(10.0, db / 20.0)`。实时和离线套用相同结果，clip gain 与 fade 继续依原有顺序相乘。旁路跟随参数 namespace，区分 clip、track、bus。整体平移节点值时对预览加短参数平滑；用户创建的阶跃插值按其明确语义保留。测试 split 时给两侧补断点并重定位右半片段，slip/retime 要定义锚定策略。首版可把包络锚定片段输出时间并对 speed 变化按比例映射，工具提示用明确模式。
4. 验收用 0 dB → −12 dB、4 s 长的包络，2 s 应为 −6 dB。取实时 renderer 与离线 PCM，分不同的 block size 对比；视图用相同节点检查 24 / 29.97 fps，1000 节点场景下拖动和缩放无明显卡顿。实测换页、切换标签、撤销、片段复制/分割/波纹剪辑、锁轨、空 lane、与时间线编辑快捷键无冲突。

## 5. 降噪、频谱修复与响度

### 噪声采集和频谱减法

1. `noisePrintCaptured(t0, t1)` 在当前有效音频版本上锁定纯噪声区间和声道。建议至少 0.25 s 或五个完整窗，取高于这一要求的区间；地区性噪声可分别存档。采样阶段不用零填充帧估计功率。保存范围、FFT 大小、hop、采样率、输入图签名与统计法版本，让重开后可以重建。调 FFT、重连或改变输入 step 后让 profile 失效，重新采集。稳定背景噪声用这一工具；瞬态点击走修复，高线谱嗡声可先试窄带 notch。 [Audacity 噪声采集工作流](https://manual.audacityteam.org/man/noise_reduction.html)
2. 对每频带算代表性噪声功率，再用 `sqrt(max(1 − α × Pnoise / Psignal, floor²))` 做软频谱减法，其中 `floor = 10 ** (−reduction_db / 20)`。在频率和时间轴平滑增益掩码，复用输入复数相位并逆变换。适度保留底噪可减轻音乐噪声。立体声输入可共用保守掩码，避免声像漂移；互不关联的多轨/多声道使用明确的分组策略。选区两侧按干湿变化做短去爆音过渡。下面是可对照测试的内存参考，建议先验证 2048 点周期 Hann 和 512 hop。生产代码用带上下文的固定块 + overlap-add 按绝对位置累积，报告进度、支持取消，首尾保持原长度。 [SciPy ShortTimeFFT](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.ShortTimeFFT.html)

```python
import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import ShortTimeFFT, get_window


def denoise_reference(
    pcm: np.ndarray,
    sr: int,
    noise: tuple[int, int],
    target: tuple[int, int],
    reduction_db: float = 12.0,
    nfft: int = 2048,
    link_stereo: bool = True,
) -> np.ndarray:
    """Small-fixture reference. PCM is finite planar float32, ranges are samples."""
    a, b = noise
    lo, hi = target
    if pcm.ndim != 2 or pcm.shape[0] == 0 or pcm.dtype != np.float32 or not np.isfinite(pcm).all():
        raise ValueError("Expected finite planar float32 PCM")
    if sr <= 0 or nfft not in (512, 1024, 2048, 4096) or not 0.0 <= reduction_db <= 48.0:
        raise ValueError("Unsupported rate, FFT or reduction")
    if not (0 <= a < b <= pcm.shape[-1] and 0 <= lo < hi <= pcm.shape[-1]):
        raise ValueError("Invalid range")
    hop = nfft // 4
    if b - a < max(round(0.25 * sr), nfft + 5 * hop):
        raise ValueError("Select a longer section of background noise")
    if reduction_db == 0.0:
        return pcm.copy()

    stft = ShortTimeFFT(get_window("hann", nfft), hop, sr, fft_mode="onesided")
    profile_spectrum = stft.stft(pcm[:, a:b])
    centers = np.rint(stft.t(b - a) * sr).astype(np.int64)
    usable = (centers >= nfft // 2) & (centers + nfft // 2 <= b - a)
    profile_power = np.median(np.abs(profile_spectrum[..., usable]) ** 2, axis=-1, keepdims=True)

    # Surround the edit with real context when available, then retain only the selection.
    block_lo, block_hi = max(0, lo - nfft), min(pcm.shape[-1], hi + nfft)
    block = pcm[:, block_lo:block_hi]
    block = np.pad(block, ((0, 0), (0, max(0, nfft - block.shape[-1]))))
    spectrum = stft.stft(block)
    signal_power = np.maximum(np.abs(spectrum) ** 2, 1e-12)
    floor = 10.0 ** (-reduction_db / 20.0)
    mask = np.sqrt(np.clip(1.0 - 1.25 * profile_power / signal_power, floor**2, 1.0))
    if link_stereo and pcm.shape[0] == 2:
        mask = np.max(mask, axis=0, keepdims=True)
    mask = uniform_filter1d(uniform_filter1d(mask, 3, axis=-2), 3, axis=-1)
    wet = stft.istft(spectrum * mask, k1=block.shape[-1])
    offset = lo - block_lo
    wet = wet[:, offset:offset + hi - lo]

    edge = min((hi - lo) // 2, round(0.005 * sr))
    mix = np.ones(hi - lo, dtype=np.float32)
    if edge:
        ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        mix[:edge], mix[-edge:] = ramp, ramp[::-1]
    out = pcm.copy()
    dry = pcm[:, lo:hi]
    out[:, lo:hi] = dry + (wet - dry) * mix
    return out
```

3. 编写纯 DSP 测试。固定随机种子，48 kHz，前 1 s 放 RMS `0.01` 的白噪声供采集；后续含另一个静噪区和 `0.2` 振幅的 440 Hz 加噪声区。默认 12 dB 模式衡量独立静噪区 RMS 下降至少 4 dB，440 Hz 估计振幅变化 ≤1 dB。分别验证 `0 dB` 逐样本旁路、选区外逐样本相同、短音频拒绝、末端裁剪、48/44.1 kHz、立体声相对相位、块拼接和原文件哈希。补对白呼吸声、尾音、齿音和突变噪声的人工 A/B，检查水声感和声像。均衡的实录样例应与算法单测分开验收。

参考实现实测使用 NumPy 随机种子 `20261118`，采集 `[0, 48000)`，处理 `[48000, 144000)`，并在 `[52800, 91200)` 测量独立噪声区。噪声 RMS 下降 5.92 dB，440 Hz 分量幅度变化 −0.005 dB。选区外、零衰减旁路和互为反极性的立体声关系通过逐样本检查。

### 频谱修复与画笔

- 选区存采样范围与 Hz。画笔持久化沿路径的 `(sample, hz)`，另外存时间半径和对数频率带宽；从像素转换后再提交，确保重开和缩放重现。填补画笔笔迹间隙，用时间和 log-Hz 椭圆距离构建连续 mask，沿边界做软化。保持 `0 Hz`、Nyquist、采样率和声道边界有效。
- 第一版实现 `attenuate`，用软 mask `gain = 1 − mask × (1 − 10 ** (−amount_db / 20))` 衰减受影响的复频谱。窄带嗡声或干扰可从未遮罩的邻近时间/频率统计估计背景上限并限制替换能量。对真实缺失内容再加 `heal`，优先沿时间从前后未受影响帧插值对数幅值，以相邻帧相位推进估计相位，再把复谱和原谱按 mask 混合；仅在置信度足够时应用。点击、爆点可另做短窗口检测与两侧波形插值，对持续片段没有可恢复信息时提供衰减预览。选区时域边缘做短干湿斜坡。
- 验收构造 440 Hz 与仅持续 80 ms 的 2 kHz 干扰，800–1200 Hz 的选区外信号和干扰前后信号保持，2 kHz 精确遮罩后能量降低至少 12 dB，500 Hz 以下信号变化 ≤1 dB。再测画笔疏密、粗细、缩放、频谱文字不可见时的快捷键、瞬态/静音、左右边界。把绘图帧归一化和 DSP 是否真的改动分开断言。

### 响度和真峰值匹配

- 调用 `analyze_loudness` 分析选区或全段有效 PCM，显示报告的 `integrated_lufs`、`true_peak_dbtp`、短段或无有效块的诊断。复用 `LoudnessMeter` 的 K-weighting 和门控，离线分析和交付读数用同一候选信号阶段以及一致的声道映射。多声道测试依标注的通道布局验证权重，未支持格式明确提示。Clip Editor 若测的是片段，应把范围与播放阶段显示在读数处。 [响度分析](../../src/features/edit/core/loudness_analyzer.py) · [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I)
- 默认增益 `g = min(target_lufs − integrated_lufs, ceiling_dbtp − true_peak_dbtp)`，保存目标、实际增益和所用选区。复测输出，峰值限制导致无法达到目标时显示可达到的响度。单独实现可选 limiter 时指定 lookahead、延迟补偿和质量策略；处理后再次验证峰值，尤其是边缘淡化、重采样和交付编码之后。
- 验收 BS.1770 已知参考信号、静音、少于 400 ms 的选区、接近 Nyquist 的高峰值信号、受峰值限制的强瞬态、含 LFE 的多通道素材和重复应用。建议初期以 LUFS `±0.2 LU`、True Peak `±0.2 dBTP` 作为测试容差，在峰值上限附近单独验证保守补偿。

## 6. 变速、音高与选区衔接

1. 明确 `ratio_pct` 是播放速度，`output_samples = round(input_samples / (ratio_pct / 100))`。无保调时使用带抗混叠重采样，变速同时改变音高。保调优先对话采用现有 `wsola_time_stretch`，通道间复用共同位移；对复音音乐/宽比率另评估带相位锁定与瞬态保留的相位声码器。单独改音高时用 `p = 2 ** (semitones / 12)` 升降调，先重采样使局部时长变成约 `N / p`，再以保调伸缩回目标时长。把边界实际样本数、组延迟和首尾衔接记录在输出映射。每种算法声明采样率、声道、比率限制和版本。 [现有 WSOLA](../../src/features/edit/core/audio_timestretch.py)
2. 选区处理在两侧读取上下文，裁掉算法延迟后按源/目标映射拼接，短交叉淡化计入最终长度。整段与局部的变速先生成候选和新时长，确认 ripple 范围、链接视频与切点语义后使用项目/序列事务。A/B 从同一映射位置试听，取消不会修改工程。默认保留视频同步的约束，用户需能明确选择独立音频编辑或同时移动链接内容。
3. 测 440 Hz、脉冲鼓点和实际对白。速度 150% 保调，输出长度误差 ≤1 sample 且主频仍为 440 Hz；单独 +12 半音的主频接近 880 Hz 且时长保持。测倒放、极短选区、立体声瞬态、长素材内存、变速后项目重开、缓存重建及成片；把短片段不支持时的反馈一并纳入。

## 7. 混音页面余项

| 优先级 | 改进 | 实施入口与验证 |
| --- | --- | --- |
| P0 | 时间线更适合音频剪辑 | 切换帧/采样尺与吸附模式，显式显示子帧位置；共用 Edit 的分割、修剪、波纹删除、倒放、音频转场和撤销快捷键。补 Windows/macOS 冲突测试及无片段/锁轨/联动视频的行为。小窗口验证 Track Header 的 R、M、S 与轨名仍能使用。 |
| P1 | 录音准备和监听 | 保留轨道头 R，增加输入设备、声道、监听和录音准备状态。复用 `RecordingManager` 设备列表和录音会话。定义单轨/多轨录制、录制输入与软件监听互斥、底层音频设备被抢占、迟到回调及权限状态。在真实设备测 48 kHz、预卷、掉线、连续 take、样本偏移和剪辑回写。 [录音管理器](../../src/features/edit/core/recording_manager.py) |
| P1 | 通道条和 Bus 路由 | 保留推子/电平样式与紧凑宽度。明确输入来源、向 Master/Bus 的输出和并行 Send 区域。切换前/后推子发送在 audio graph 生效，Bus 重命名按 ID 稳定，删除时提示受影响的轨道与 sidechain。用独立音频信号测 mute/solo、solo safe、相位、pan/width、环路诊断及 undo。 |
| P1 | 自动化、插槽、监听范围 | 从现有模式逐项接入 Read/Touch/Latch/Write、FX bypass/reorder、效果预设、参数手势和延迟补偿。轨道音频图和离线调用同一参数采样。监听 Mono/DIM 与监听电平留在监控状态，验证导出 PCM 一致。 |
| P2 | Meter 和交付 | 聚合轨道、Bus 与 Master 的 Peak/RMS/GR/peak-hold/削波灯，跨页只绑定一次并按 UI 刷新率节流。24/29.97 下测试 Master、DX/FX/MX Stem 同起点、尾音、Bus 后与 Master 前后的明确取点，交付预检显示缺处理器、离线素材和峰值问题。 |
| P2 | 工作区和可访问性 | 考察 Media Pool/Effects 合组、窄屏预览、4K/Retina 波形缩放、双显示器浮动窗口恢复、键盘移到标签和控件、屏幕阅读器名称与当前选择反馈。测试录音/试听过程中切页，窗口退到不可见时清理资源。 |

## 8. 交付验收与回归矩阵

| 层级 | 验收方式 |
| --- | --- |
| 文件与历史 | 原素材 SHA-256 始终相同。保存、重开、另存、包搬家、重连、缺源、未知步骤版本、取消和缓存清理。恢复后选区、处理顺序、实时播放与离线导出同一结果。 |
| 音频管线 | 用内置合成 fixture 在相同取点比较 `Edit Playback`、`Mixer Playback`、`Clip Editor Audition` 和 `Deliver`。对比轨道图前先隔离轨道并关闭 Master 监听项。相同输入图、采样率和 DSP 路径用通道逐样本误差 `≤1e−5` 作为浮点基准，重采样和有损编码单立容差。 |
| 体验 | 双击片段进入编辑，选中后点 Clip Editor 标签自动加载，返回时间线后还能直接切回。两页互切保留素材目录、序列和效果目标；浮动、合并与窄屏检查波形、时间尺和右侧控制是否可操作。 |
| 性能 | 30 分钟立体声和 90 分钟多轨回归，记录峰值内存、音频欠载、缓存命中、拖动帧耗时、首波形/首声音延迟。长任务取消后无挂起 worker、遗留工件或重复音频设备，预览不争抢实时音频线程。 |

建议首轮回归命令。处理栈和 DSP 有对应实现后再追加专项 fixture。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/mixer tests/edit/test_audio_graph_parity.py \
  tests/edit/test_audio_export_parity.py tests/edit/test_sample_time_audio.py \
  tests/edit/test_fade_curve.py tests/edit/test_audio_automation.py \
  tests/edit/test_edit_effects_panel_drag_inject.py tests/edit/test_loudness_analyzer.py
```

补做原生 UI 实测，在带时间码的对白、音效、音乐三个素材上完成至少一次 Edit 导入、Mixer 片段处理、轨道发送、返回 Edit 验证、保存重开以及 Master/Stem 导出。用 PCM 和工程差异作为验收附件，截图用于核对布局与交互。
