# Topos Studio 音频效果器后端实施计划

2026-11-21 · 覆盖 23 款效果器与音频片段编辑器

“现状”来自当前工作区。各节的新字段名、默认值、算法选择和数值门槛为实施建议。每个批次先重跑现有用例，再把采用的合同写进新测试。代码示例用于学习、复现和建立独立验算，生产接入位置见各节实施步骤。 [面板来源](../../src/features/mixer/effects/source_manifest.json) · [原型参数](../../src/features/mixer/effects/parameter_schemas.json) · [经典参数映射](../../src/features/mixer/effects/classic_bindings.py) · [机架参数映射](../../src/features/mixer/effects/rack_bindings.py)

现有处理器从新版面板的功能卡读取参数。后续扩展也在对应功能卡内增加控件或补齐接线，并使用同一试听和提交流程。Qt 根据可见控件计算窗口所需空间，新增控件时同时检查紧凑尺寸和两种语言下的完整显示。

## 从哪里开始

1. 在项目根目录用项目 Python 环境运行下面的基线命令。macOS/Linux 使用 `.venv/bin/python`。Windows 使用 `.\.venv\Scripts\python.exe`，在 PowerShell 先设置 `$env:QT_QPA_PLATFORM = "offscreen"`。
2. 从索引选一行，把该节 Python 示例复制到临时 `.py` 文件，用项目解释器运行。包含 `src` 导入的示例从项目根目录运行，例如 `PYTHONPATH=. .venv/bin/python /tmp/fx_check.py`。确认断言通过后，用该节的正弦、脉冲或短素材固定旧行为，再开始改参数及处理器。
3. 一次完成一个效果器的最小版本。顺序为算法单测 → 分块与复位 → 生产链 → 工程和交付 → 计量/UI。复制测试工程与音源保存旧基线。记录各项通过、跳过及实机结果，直接使用文末的交付记录。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_audio_effect_registry_health.py \
  tests/edit/test_audio_dsp.py \
  tests/edit/test_audio_e2e_new_fx.py \
  tests/edit/test_clip_fx_pdc.py \
  tests/shared/test_effect_catalog_contract.py \
  tests/mixer/test_effects_panel.py
```

## 逐项实施索引

| 优先级 | 面板 | 当前接入 | 首个后端任务 |
| --- | --- | --- | --- |
| P2 | [FX-01 参数均衡器](#fx-01) | `strip_eq` 六段 | 核对滤波响应并加入分实例真实频谱。 |
| P1 | [FX-02 动态均衡器](#fx-02) | `dynamic_eq` 单段 Bell | 固定单段兼容，再为每段接检测器、系数与侧链。 |
| P2 | [FX-03 动态处理器](#fx-03) | `dynamics`、`compressor`、`expander`、`noise_gate`、`limiter` | 校准三段传递曲线与实际增益衰减，补片段侧链。 |
| P1 | [FX-04 多段压缩器](#fx-04) | `multiband_comp` 三段 | 统一增益补偿键与时间参数，保留历史声音。 |
| P1 | [FX-05 总线压缩器](#fx-05) | `bus_comp` 胶合模板和侧链高通 | 记录旧模板控制曲线，再开放连续压缩参数。 |
| P1 | [FX-06 齿音消除](#fx-06) | `de_esser` Wide/Split | 增加检测形状与前视。 |
| P1 | [FX-07 去点击/去嗡声](#fx-07) | 独立离线处理器 | 接片段选区、诊断和差分试听。 |
| P2 | [FX-08 音高校正](#fx-08) | `pitch_correction` 面板 | 先做可取消的单声部 F0 追踪与音阶量化。 |
| P2 | [FX-09 延迟](#fx-09) | `delay` 时延和反馈 | 对拍读头与尾音，核对路由后扩声道布局。 |
| P2 | [FX-10 算法混响](#fx-10) | `reverb` 房间、反射和尾音 | 分别测直达、ER 和 RT60，明确 IR 图数据源。 |
| P0/P1 | [FX-11 卷积混响](#fx-11) | `convolution_reverb` 基础文件导入和卷积 | 修复加载/清空、核对实际延迟和四路径资源映射。 |
| P2 | [FX-12 空间声像](#fx-12) | `spatial_panner` 轨属性 | 按实际布局核对坐标、路由和 LFE。 |
| P1 | [FX-13 M/S 立体声成像](#fx-13) | `imager` Width/Pan | 加入 M/S 独立增益和 Mono Maker。 |
| P1 | [FX-14 饱和](#fx-14) | `saturation` 单段 sigmoid | 保留旧 Drive，给新模型加限带非线性。 |
| P2 | [FX-15 激励器](#fx-15) | `exciter` 面板 | 先做可独立测量的两段谐波支路。 |
| P2 | [FX-16 次谐波](#fx-16) | `subharmonic` 面板 | 做单音跟踪、八度生成和失锁门控。 |
| P1 | [FX-17 瞬态塑形](#fx-17) | `transient_designer` 攻击和延音 | 定义百分比到 dB 的曲线，再补侧链和输出。 |
| P1 | [FX-18 合唱/镶边/移相](#fx-18) | `chorus_flanger`、`phaser` 独立实例 | 先验证读头延迟和跨块状态，再接三模块。 |
| P1 | [FX-19 颤音/自动声像](#fx-19) | `tremolo_auto_pan` 正弦 LFO | 建立波形、拍点、相位与 seek 合同。 |
| P2 | [FX-20 加倍/和声](#fx-20) | `doubler` 面板 | 先做双声、延迟和声像，再接音程与调式。 |
| P2 | [FX-21 变声](#fx-21) | `voice_changer` 三模块 | 校准 F0、共振峰测量和各模块计量。 |
| P2 | [FX-22 频谱变形](#fx-22) | `spectral_mangler` 三模块与 Freeze | 跑通 Freeze 资源发布、重开和确定性渲染。 |
| P0/P1 | [FX-23 真峰限制器](#fx-23) | `true_peak_limiter` 4×、前视、释放 | 验证声道链接与天花板，补 LUFS 和 Dither。 |
| 专项 | [FX-24 音频片段编辑器](#fx-24) | 已有工作 PCM、波形、频谱、试听和栈 | 从既有验收版扩处理器，复验真机与交付一致性。 |

推进顺序为 P0 卷积与真峰 → 共享状态、版本与测试支架 → P1 补齐已有处理器 → P2 新处理器及计量。FX-15 复用 FX-04/FX-14 的分频和过采样，FX-20 可复用 FX-08/FX-21 验过的变调核心，FX-07/FX-08 的离线操作复用 FX-24 的片段事务。

## 公用实施合同

| 步骤 | 改动落点 | 交付证据 |
| --- | --- | --- |
| 1 · 参数 | 在 `timeline_v2.py` 扩展实际 Params 类型或建新类型，实现 `default/from_dict/to_dict` 并接入 `_ensure_effect_param_registry`。每个键写清单位、默认、范围、缺省/坏值、旧值读取、平滑和自动化。UI 快照经映射转换，生产模型只保存工程字段。 | 旧档、新档、未来未知字段、NaN、非法枚举和自动化回填。确认每个键确被处理器消费。 |
| 2 · 处理器 | 在 `audio_effects/` 实现独立 DSP，参照 `adapters.py` 接到 `audio_dsp._ensure_effect_registry`。先直测单元，再挂 `EffectChain`。轨道声像沿 `SpatialPannerParams` 与轨道属性链路，离线修复沿 `audio_edit.PROCESSORS`。 | 同样的输入在无 QWidget 环境仍可处理，注册表健康且实际链能运行。 |
| 3 · 版本 | Track 的 `TrackEffectInstance.algorithm_version`、序列 `audio_fx_schema_version` 和缓存版本各自记录不同变化。DSP 全局版本当前为 4。保持历史 PCM，声音改变走独立版本或明确升级命令。Clip 的 `audio_retime.clip_fx_entries` 当前生成 3 元组，Track 的条目携带实例 ID 和版本。依赖这两项的新算法先贯通 Clip 试听、动画采样及交付分支，并保留旧 Clip PCM 基线。 | 老档可以重存，未知字段和未来版本有诊断，两个同类型实例调序不串状态。 |
| 4 · 开放能力 | 核对 `EffectRegistry`、`AUDIO_MIXER_EFFECT_TYPES`、后端工厂与实际支持的 Clip/Track/Bus/Master。依赖缺失、坏资源及不支持的布局走可见诊断与导出预检。确认后再把相应控件加入 `PRODUCTION_BINDINGS`。 | 成功插入并预览和导出、缺资源失败、未开放作用域反馈。 |
| 5 · 目录 | 生产目录字段变更后运行 `src.shared.contracts.effect_catalog.write_catalog_file()` 重新生成入库 JSON。查看差异并同步契约测试冻结的 `catalog_digest()`。 | 生成产物与干净子进程重建结果相同，参数默认和范围自洽。 |

[生产注册表](../../src/features/edit/core/effect_registry.py) · [参数与自动化](../../src/features/edit/core/timeline_v2.py) · [版本门](../../src/features/edit/core/fx_versioning.py) · [处理器工厂](../../src/features/edit/core/audio_dsp.py) · [DSP 适配器](../../src/features/edit/core/audio_effects/adapters.py) · [共享目录](../../src/shared/contracts/effect_catalog.py) · [目录测试](../../tests/shared/test_effect_catalog_contract.py)

- **PCM。** 输入/输出采用 `(channels, samples)` 平面浮点 PCM。处理器保持约定的通道与块长度，不原地修改 decoder 输入；测试 0、1、非整 hop 大小和超峰浮点数据。声道布局用 one-hot 脉冲核对，输出量化留在交付层。
- **状态。** 以序列、作用域、实例 ID 与采样率隔离环形缓冲、滤波 `zi`、相位和随机状态。普通参数调节保持状态并渐变。重复同配置应幂等，seek、停止、删除与换率分别测试复位、预卷和旧结果清理。
- **延迟与尾音。** `latency_samples()` 是算法造成的整体时间偏移，`tail_estimate_samples()` 是停输入后需要馈零的样本数。混响前置时间、调制回声等按效果定义和实际冲激响应区分。先对齐效果内部干湿，再由实时和离线图做跨轨 PDC。Stem 选择区间另测预卷和尾巴。 [实时混音](../../src/features/edit/core/audio_playback.py) · [离线混音图](../../src/features/edit/core/audio_graph_renderer.py) · [片段 PDC 回归](../../tests/edit/test_clip_fx_pdc.py)
- **资源。** 音频设备回调避免阻塞 I/O、UI 调用和不可控分配。IR/F0/Freeze、长素材与 FFT 准备走可取消 worker；回包带工程、实例和 generation。工程存可迁移的引用、版本与指纹，临时文件按作业管理，完整验证后原子发布。关闭、失败和取消走清理路径。 [PortAudio 回调约束](https://files.portaudio.com/docs/v19-doxydocs/portaudio_8h.html)
- **交互。** 新面板预览使用引擎快照，应用走时间线或机架命令，取消还原模型。遥测优先复用 `EffectChain.tap_levels()`，按真实实例输出输入/输出峰、GR、检测与 analysis。图区分预测曲线与测量；测量按订阅限频，切片、暂停、旁路清除过期读数。 [生产面板](../../src/features/mixer/effects/production_dialog.py) · [片段对话框](../../src/features/edit/ui/clip_audio_effect_dialog_adapter.py) · [控制器](../../src/app/controllers/edit_controller.py)

### 第一条 DSP 测试怎样写

下面通过项目实际的 `EffectChain` 运行现有饱和器。开始下一款时，换成该类型的生产参数和各节的刺激信号，接着补该节的响应、延迟与尾音断言。

```python
import numpy as np

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain

fs = 48_000
rng = np.random.default_rng(290)
pcm = rng.uniform(-0.3, 0.3, (2, 4001)).astype(np.float32)
original = pcm.copy()

def render(x, block_size, enabled=True):
    chain = EffectChain(fs)
    chain.set_params([
        ("saturation", {"drive": 35.0, "enabled": True},
         enabled, "fixture-saturation", ALGORITHM_VERSION),
    ], sample_rate=fs)
    blocks = [chain.process(x[:, start:start + block_size])
              for start in range(0, x.shape[-1], block_size)]
    return np.concatenate(blocks, axis=-1)

whole = render(pcm, pcm.shape[-1])
assert whole.shape == pcm.shape and np.isfinite(whole).all()
assert np.max(np.abs(whole - pcm)) > 0.01
for size in (1, 127, 513, 1024):
    np.testing.assert_allclose(render(pcm, size), whole, rtol=0, atol=1e-6)
np.testing.assert_array_equal(render(pcm, 513, enabled=False), pcm)
np.testing.assert_array_equal(pcm, original)
```

## 每个批次的验证顺序

| 阶段 | 操作和通过条件 |
| --- | --- |
| A · 数学 | 先用 48 kHz 静音、单位脉冲、稳态正弦和固定 seed 噪声。各节的数字是指定夹具的标准。频谱测量用相同窗和稳定段，注明幅度是线性、dBFS、dBTP、相对 dB 还是 LU。 |
| B · 分块和性能 | 核对 44.1/48/96 kHz、支持的 mono/stereo/多声道、127/513/1024 和设备实际块长。覆盖尾端短块、采样率变化、seek、旁路和状态切换。初始 float32 数值测试可从 `atol=1e-6` 起步；有算法时延的测试先对齐。测单实例和目标并发，记录 P95/P99、缓存/RSS 与 underrun。每块设备时间预算为 `block_size/fs` 秒。 |
| C · 工程和异常 | 走真实 UI 插入、预览、提交、旁路、排序、撤销、重做和保存重开。每项各验两个同类型实例、锁轨、换序列与设备，资源型补损坏、丢失、同路径替换、迁移与取消。以旧项目 PCM 固定兼容。 |
| D · 交付 | 在相同版本、参数与采样率下对照片段试听、实时混音浮点 PCM、成片和 Stem，记录样本对齐、开始/结束、尾音和 PDC。关闭 Dither 的 24-bit PCM 以 1 LSB（`2**-23`）量级作量化对照。存在 Dither 则用固定 seed 或统计门槛。 [成片入口](../../src/features/edit/core/timeline_export.py) · [Stem 入口](../../src/features/edit/core/stem_export.py) · [对拍用例](../../tests/edit/test_audio_export_parity.py) · [预检](../../tests/edit/test_audio_export_preflight.py) |
| E · 实机 | 用对白、音乐和打击乐短素材按等响 A/B。走播放调参、预览取消、seek、旁路和应用。记录 macOS/Windows 的设备、块长、采样率和结果。验证报告单列已通过和待实机的条目。 |

计量与独立参考数据按所采用的标准版本和布局记录，保留输入文件对应的测试条目。 [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I/en) · [EBU Tech 3341](https://tech.ebu.ch/docs/tech/tech3341.pdf) · [EBU 测试音频](https://tech.ebu.ch/publications/ebu_loudness_test_set) · [RBJ 双二阶公式](https://webaudio.github.io/Audio-EQ-Cookbook/audio-eq-cookbook.html)

## 逐项实施与验收

<a id="fx-01"></a>

### FX-01 参数均衡器（P2）

**现状。** `strip_eq` 已有 6 段、HPF/LPF、低/高架、Bell、Notch、各段启用/独奏、主增益和四种 Voicing。音频处理在 `StripEQFilter`，`strip_eq_band_sections()` 生成二阶节系数，`strip_eq_band_response_db()` 用同一组系数求响应。新版窗口已可操作实际参数，当前优先补不同作用域的实例频谱与图形一致性。 [处理器与系数函数](../../src/features/edit/core/audio_dsp.py) · [EQ 面板](../../src/features/mixer/effects/panels/eq.py)

| 项目 | 实施合同 |
| --- | --- |
| 身份和作用域 | 既有 `strip_eq` ID；Clip、Track、Bus、Master 分别按实际渲染实例取数。轨条 EQ 徽标与通用插槽可以指向不同实例。 |
| 幅度和滤波 | 六段 `freq` 为 Hz，`gain` 与 `master_gain` 为 dB，`q` 无量纲。HPF/LPF 的 `slope` 是 6/12/18/24 dB/oct。频率上界由当前采样率约束。 |
| 数值默认 | `StripEQParams.default()` 为 Clean、主增益 0 dB、HPF/LPF 旁路、四段无增益修改。`range_key` 属于编辑步进。 |
| 兼容和路由 | v0/v1 继续旧斜率级联和固定货架 S，v2 使用 Butterworth 和 Q，v3+ 保留现行增益/Q 耦合。历史数值数组按现有模型规则读入。新增频谱沿用算法版本。 |
| 图形两类数据 | 曲线用生产系数以当前 fs 和算法版本求预测值；入/出频谱用实例实测 PCM。旁路时曲线 0 dB，遥测独立标旁路清表。 |

1. 先锁定 `test_strip_eq.py` 与 `test_audio_r01_version_compat.py` 的旧数据和响应。给新版 EQ 曲线读取器传 `sample_rate` 与 `algorithm_version`，只在频段、旁路、Voicing 或有效采样率变化时更新频率网格。对六段启用及 Solo 规则，复用 `StripEQFilter` 的选择逻辑。
2. 在 `EffectChain.process()` 的已存在实例 tap 扩展订阅型 PCM 支路。先把 Clip 的链条核实到具备实例 ID，再把 Track/Bus/Master 同类型实例连接到对应窗口。采集每实例有限输入/输出窗和已有 `in_db/out_db`，关窗解除订阅。当前 `audio_retime.clip_fx_entries()` 生成三元组；单独验证将 Clip 的稳定实例 ID 与版本传至链后所用的历史版本桶，预览、导出、动画采样在同一处扩展。
3. 在音频线程复制已预分配的小块到有界队列；FFT、窗函数和绘图点压缩由 worker 做。投递包含工程/时间线、对象、实例、采样率、分析序号和 generation。录放停、seek 或替换设备时清窗；UI 只读快照、只发参数命令。
4. 六段整串计算 `H_total = linear_master_gain × ∏H_band`；画 dB 时可以相加各段幅度 dB。预测图与实时 FFT 窗输出使用不同 labels，避免把当前曲线的增益和本底素材频谱混作同一读数。 [W3C Audio EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/) · [实时混音和 generation](../../src/features/edit/core/audio_playback.py) · [Clip 链条目构建](../../src/features/edit/core/audio_retime.py)

**实验脚本。** 在仓库根用项目 Python 运行。通过独立的频率响应求值核对生产生成的 SOS 行，再核对可预知的中心增益。

```python
import numpy as np
from scipy.signal import sosfreqz

from src.features.edit.core.audio_dsp import (
    strip_eq_band_response_db,
    strip_eq_band_sections,
)

fs = 48_000
band = dict(filter_type="Bell", freq=1000.0, q=1.1, gain=6.0, slope=12)
frequencies = np.array([100.0, 1000.0, 8000.0])
sections = np.asarray(strip_eq_band_sections(band, fs, "Clean", 3))
_, response = sosfreqz(sections, worN=frequencies, fs=fs)
reference_db = 20 * np.log10(np.abs(response))
chart_db = [strip_eq_band_response_db(band, f, fs, "Clean", 3) for f in frequencies]
np.testing.assert_allclose(chart_db, reference_db, rtol=0, atol=1e-8)
assert abs(reference_db[1] - 6.0) < 1e-8
```

**自动验收。** 扩展旧测试，覆盖每个滤波形状、每档斜率、各 v 桶与 44.1/48/96 kHz。先对平直模式和旁路做严格逐样本比较，再对曲线与稳定扫频的中段幅值设 0.2 dB 初始门槛；极深 Notch 单独以数值地板定义公差。快速拖 Q/频率后采集下一块实测频谱，检查停止/换工程无残留、同轨两个 EQ 实例数据不串。 [EQ 回归](../../tests/edit/test_strip_eq.py) · [版本回归](../../tests/edit/test_audio_r01_version_compat.py)

**首次修改前的自检。** 先跑 EQ 和版本基线，再改单个图形入口。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_strip_eq.py tests/edit/test_audio_r01_version_compat.py
```

**手动验收。** 把 1 kHz 测试音放入片段，Clip EQ 在中心加 6 dB，轨 EQ 保持 Flat。查看前者曲线和输出频谱均抬升且后者 tap 正确捕获上游变化。关窗和 seek 后看清表；降低主增益以免试听削波。导出 32-bit float WAV、按相同采样率比对有效区间，再记录实例 ID 和实测增益。

<a id="fx-02"></a>

### FX-02 动态均衡器（P1）

**现状。** `DynamicEqEffect` 用内检源、50 ms 移动 RMS、单段 RBJ Bell、限定 `max_cut_db/max_boost_db` 的阈值调制。生产单段参数对应新面板第 4 段，静态路径与 Strip EQ 的同版 Bell 对拍。`GAIN_BLOCK=512` 当前在每次 `process()` 内重新分段。多段、自动时序和外部检源需要独立实现。 [动态 EQ 处理器](../../src/features/edit/core/audio_effects/dynamic_eq.py) · [机架字段映射](../../src/features/mixer/effects/rack_bindings.py) · [面板](../../src/features/mixer/effects/panels/dynamic_eq.py)

| 项目 | 建议生产合同 |
| --- | --- |
| 版本与迁移 | v0–v4 原样进入旧单段路径。显式升级将旧频率/Q/静态增益/阈值/时间/启用写到稳定第 4 段，保留上下两界，其他段初始化为关闭。 |
| 参数 | `frequency_hz`、`q`、`static_gain_db`、`threshold_dbfs`、`max_cut_db`、`max_boost_db`、`attack_ms`、`release_ms`。`filter_slope_db_oct` 与动态 `sensitivity` 分开存。 |
| 听检与侧链 | `trigger_mode` 先定义 above、below、both。`sidechain_source_id` 用工程 ID。上下界都是非负幅度；听检用临时预览，移除检源回内部并发一次诊断。 |
| 兼容默认 | 新增的其余段均关闭，内检源、最小相位、0 固定样本延迟、主增益 0 dB；保留旧版单段 3 kHz、Q 1.4、阈值 −30 dBFS、限幅 −9/+0 dB、攻击/释放 8/120 ms。 |
| 监听范围 | 第一批 Clip/Track/Bus/Master 的内检源，第二批才放行各作用域外检。近线/线性相位使用单独模式标记和预卷合同。 |

1. 在 `timeline_v2.py` 给新版本加含稳定 band ID 的数组。读取旧 dict 仍维护既有扁平自动化键；升级命令用表驱动迁移可迁移 lane，并让 undo 复原原参数和原版本。新 lane 使用稳定 band ID，禁止对数组长度、滤波器类型和源 ID 做连续插值。
2. 在 `DynamicEqEffect` 新版支路创建每段独立的 detector、包络和输出滤波状态。先规定所有检源取未处理输入的各段滤波结果；输出再依固定 band ID 顺序串联。用同声道功率聚合避免反相信号在检源取平均时抵消。段 Solo 只作为监听。
3. 在任意块长下按持续样本时钟更新动态系数。例如每 128 个样本更新一次目标，以上一有效状态推至下一边界。滤波器拓扑变化要准备新系数并以短交叉淡化切换；同拓扑小幅变化连续保留状态。攻击/释放判断逐样本用当前包络值。
4. 外检沿用 FX-03 的依赖图、执行顺序和缺源回退。输出低延迟版通过后，才实现 FIR 固定群延迟与音频/干路同步，再接 `latency_samples()` 给实时和导出的 PDC。UI 上的预测频响和每段实测 `detector_dbfs/dynamic_gain_db` 分开供数。
5. 先在生产绑定表用单段和新版多段区分控件映射。状态初始化走同一模型入口，窗口只提交稳定 ID 的参数，`set_params()` 及系数/滤波状态保留在链实例。

**实验脚本。** 给包络电平输入验证目标增益、上/下两界及时序。这是控制律参考，RBJ 滤波器由生产 helper 单独验证。 [W3C Audio EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/)

```python
import numpy as np

def target(detector_dbfs: np.ndarray) -> np.ndarray:
    over = detector_dbfs - (-30.0)
    return np.clip(-0.5 * over, -9.0, 3.0)

def smooth(values: np.ndarray, state: float, fs: int = 48_000) -> tuple[np.ndarray, float]:
    out = np.empty_like(values)
    for i, goal in enumerate(values):
        tau = 0.008 if goal < state else 0.120
        a = np.exp(-1.0 / (fs * tau))
        state = a * state + (1.0 - a) * goal
        out[i] = state
    return out, state

np.testing.assert_array_equal(target(np.array([-50., -30., -20., 0.])), [3., 0., -5., -9.])
values = target(np.repeat([-50., -20., -50.], 4000))
whole, _ = smooth(values, 0.0)
a, carry = smooth(values[:1031], 0.0)
b, _ = smooth(values[1031:], carry)
np.testing.assert_array_equal(np.r_[a, b], whole)
assert -5.0 < whole[7999] < -4.9
```

**自动验收。** 先用同版静态 Bell、旧单段响度/波形对拍。新实现用 1 kHz 与 6 kHz 双音驱动不同频段，分别量出阈值变化、限幅及另一频段的泄漏。测试 257/511/1024 样本块与整段，测试规定新版本同样本时钟下最大误差门槛 1e−5；遇到拓扑切换记录有效延迟和交叉淡化过冲。 [既有 Dynamic EQ 回归](../../tests/edit/test_dynamic_eq.py) · [Clip FX PDC 回归](../../tests/edit/test_clip_fx_pdc.py)

**首次修改前的自检。** 先跑静态 Bell 兼容和片段延迟基线，新增断言应指明是旧单段版还是新多段版。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_dynamic_eq.py tests/edit/test_clip_fx_pdc.py
```

**手动验收。** 把两段 Bell 分别放 1 kHz、6 kHz，调快 attack 并把后者最大衰减设为 6 dB。播放响/轻两段，查看仅 6 kHz 的控制曲线启动；切换侧链、Solo 后关窗，再从同一时间点导出听感与表计证据。

<a id="fx-03"></a>

### FX-03 动态处理器与独立模块（P2）

**现状。** 片段级 `dynamics` 在一个 `DynamicsEffect` 内按扩展/门限 → 压缩 → 限幅 → Make Up 顺序运行；轨条 DY 组用三条 `panel="dynamics"` 的实例，独立插槽也可保存各模块。`noise_gate` 是旧版门限入口。轨/Bus/Master 已有外检 tap、路由图和实例级压缩表计；现有 Clip 链的 `process(samples)` 未接收外部 sidechain map。 [现有处理器与 EffectChain](../../src/features/edit/core/audio_dsp.py) · [实时混音](../../src/features/edit/core/audio_playback.py) · [离线图](../../src/features/edit/core/audio_graph_renderer.py)

| 参数或边界 | 实施合同 |
| --- | --- |
| 扩展/门限 | `eg.mode` 继续 Gate/Expander，阈值 dBFS、`range_db` 为最大衰减的正 dB、Ratio 无量纲。旧 `noise_gate` ID 与 Gate 读取规则保留。 |
| 压缩 | 阈值 dBFS、Ratio 无量纲、Knee 和并行 Mix 仍使用旧式 0–100%、`makeup_gain` 为 dB，`sc_source` 保留实际 track ID。 |
| 各段时序 | `attack_ms/hold_ms/release_ms` 均以 ms 进入现有作用域，换采样率后重算内部系数和计数器。子电源与轨组总电源分别储存。 |
| 补偿顺序 | 片段组合顶层 `makeup` 是限幅后的增益；轨三件套内既有 `makeup_post` 明确区分组前/组后。历史未标注值继续沿用旧位置。 |
| 新增交互语义 | 主可视曲线根据本次三段状态求预期值；E/G、C、L 三根表展示各自非负 GR，不用最大值填满三根表。 |

1. 先补 `DynamicsEffect.collect_telemetry()`，按子模块输出实际块内 GR、检测电平和限幅过冲。独立模块复用 `EffectChain.tap_levels()`，组后补偿的出表应取补偿后的样本。记录表计到底是峰值还是块末状态，旁路和关闭皆清零。
2. 沿 `audio_playback.py` 和 `audio_graph_renderer.py` 的 Track/Bus sidechain tap 查找顺序，把 Clip 的需求加入路由图。在片段一侧每块消费已对齐的 tap。Clip 在所属轨进入其他 FX 之前完成处理，所以同轨自检源沿用内部检测；跨轨循环报告诊断并回退内部。
3. 检源历史以 `track_id` 绑定，改名只改显示，删除只标断开。默认建议缺源回内部并显示状态，避免隐性静音。源通道和块长度在混音/导出各自统一，传输整个音频块；听检播放检源原极性，正式输出不采纳临时监听开关。
4. 窗口发预览与单笔提交；音频线程保留 `_env_lin`、hold 计数、声道状态。参数微调不得新建现有子实例；seek 清状态。PDC 使用整链声明；复合片段与轨三实例用相同 Make Up 版本对拍。
5. 曲线基准值优先调用处理器的纯函数/计算函数；独立试验使用硬拐点。对每个生产算法版本分别保留旧声音夹具。 [AES 压缩器设计论文作者稿](https://eecs.qmul.ac.uk/~josh/documents/2012/GiannoulisMassbergReiss-dynamicrangecompression-JAES2012.pdf)

**实验脚本。** 这个模型只验稳态电平和串联顺序，采用 Gate 硬切、硬拐点压缩、瞬时理想限幅。完整包络在生产处理器测试。

```python
from math import isclose

def gate(level_db: float, threshold_db: float, depth_db: float) -> float:
    return level_db - depth_db if level_db < threshold_db else level_db

def compress(level_db: float, threshold_db: float, ratio: float) -> float:
    if level_db <= threshold_db:
        return level_db
    return threshold_db + (level_db - threshold_db) / ratio

def stages(level_db: float) -> tuple[float, float, float, float]:
    gated = gate(level_db, -35.0, 18.0)
    compressed = compress(gated, -18.0, 3.0)
    limited = min(compressed, -14.0)
    return gated, compressed, limited, limited + 6.0

assert stages(-40.0) == (-58.0, -58.0, -58.0, -52.0)
assert stages(0.0) == (0.0, -12.0, -14.0, -8.0)
assert isclose(stages(-6.0)[-1], -8.0)
```

**自动验收。** 以 DC 台阶或经校准的正弦包验证 Attack/Hold/Release，逐样本记录事件。建议硬膝充分稳定后电平误差 ≤0.2 dB，Hold 边界与声明样本数相差 ≤1。对同一序列仅改路由对象，比较 Clip/Track/Bus/Master 的检源选择与 GR。再测源删除/改名、侧链循环、采样率切换、关窗以及遗留 `noise_gate`。 [处理器及侧链回归入口](../../tests/shared/test_audio_mx04_mx07_mx08_engine.py)

**首次修改前的自检。** 修改路由前保存这组输出。增补片段外检案例时，检测波形可直接使用具有明确极性的方波。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/shared/test_audio_mx04_mx07_mx08_engine.py tests/edit/test_clip_fx_new_effects.py
```

**手动验收。** 用对话压音乐。先给压缩器设置本轨不触发的阈值，选择旁轨实测讲话时 GR 上升。Solo 检源应听到原声，关闭面板后正式试听回正常。逐次锁轨、改名、移除旁轨，验证项目保存重开时已有路由可诊断。

<a id="fx-04"></a>

### FX-04 多段压缩器（P1）

**现状。** `multiband_comp` 用两个 `_LR4Section`，每级用四阶低通分出 Low，再以 `input - low` 得到余带，然后各段交 `CompressorEffect`。当前精确重构是减法恒等式；传统 LR4 高/低通并联的相位合同另算。现有 `set_params()` 会重建分频和压缩状态，`_rebuild()` 写死两声道。 [多段处理器与模型](../../src/features/edit/core/audio_effects/bus_multiband.py) · [生产参数载体](../../src/features/edit/core/timeline_v2.py) · [Linkwitz 原始分频说明](https://www.linkwitzlab.com/crossovers.htm)

| 项目 | 建议生产合同 |
| --- | --- |
| 分频 | 第二版以 `band_count` = 2/3/4、升序 `crossovers_hz` 长度 = 段数减一；内部 clamp 频率至低于 Nyquist 的验证上界；非法交叉点报错。 |
| 段 | 稳定 `band_id`、`threshold_db`（dBFS）、`ratio`、`knee`（兼容现有 % 定义）、`attack_ms/release_ms`、`makeup_gain`（dB）和分段电源。 |
| 优先关系 | 新版每段显式 Attack 优先，缺省时继承全局 `attack_ms`。旧版消费已存在的分段 Attack，无值沿用旧处理器的 10 ms。 |
| 保存兼容 | 当前 defaults 写 `makeup_db`，实际 `CompressorEffect` 读 `makeup_gain`。旧项目原样运行；显式升级默认只搬已经生效的值，另行确认是否激活旧的未生效值。 |
| 相位和监听 | 第一批保留减法互补及 0 固定延迟；四阶并联、其他斜率或线性相位分别声明。Solo 是临时监听，Mute 是明确允许提交的效果参数。 |

1. 先加入三段全中性的 PCM 重构回归和典型工程旧版 PCM 夹具。明确 `makeup_gain`、全局 Attack 与各段 Attack 的采纳顺序，把别名兼容集中于参数入口 `MultibandCompParams` 新版本路径。
2. 将写死 2ch 改为依据 DSP 输入实际通道数设置 SOS `zi`。`_rebuild()` 只在采样率/通道数/频段拓扑变化时整体执行。相同拓扑的旋钮提交复用已有 CompressorEffect 包络，分频点更新可用旧、新系数短交叉淡化。预先准备系数和工作数组。
3. 在 `EffectChain` 恢复量化字段及稳定跨度后拓展 N 段：递归对余带分频，应用各段 GR，按固定次序求和，最后施加输出补偿。分段 `enabled=false` 时该段仍参加重构，`mute` 则将该段采样归零。
4. 新版 FIR 模式先规定每段同一固定延迟并让参考干路也延迟同样样本数，导出和实时收到同一 `latency_samples()`。确认已有 PDC 与 feed-zero drain 获得满足后再开放模式。
5. 用 `collect_telemetry()` 填每段真实 GR，带上 band ID 和本次处理频率、相位构型，避免段数变更后旧数据错位。 [机架绑定表](../../src/features/mixer/effects/rack_bindings.py) · [多段面板](../../src/features/mixer/effects/panels/multiband_compressor.py)

**实验脚本。** 用四段的减法互补验证任意声道及重构。采用整段计算的实验实现；生产保留滤波状态和缓冲。

```python
import numpy as np
from scipy.signal import butter, sosfilt

def split(signal: np.ndarray, crossovers: tuple[float, ...], fs: int) -> list[np.ndarray]:
    residual = signal.copy()
    bands = []
    for frequency in crossovers:
        section = butter(2, frequency, btype="lowpass", fs=fs, output="sos")
        low = sosfilt(np.vstack([section, section]), residual, axis=-1)
        bands.append(low)
        residual = residual - low
    return [*bands, residual]

rng = np.random.default_rng(42)
x = rng.normal(0, 0.1, size=(6, 10_000))
bands = split(x, (120., 800., 4200.), 48_000)
assert len(bands) == 4 and bands[-1].shape == x.shape
np.testing.assert_allclose(np.sum(bands, axis=0), x, rtol=0, atol=1e-14)
```

**自动验收。** 先做 mono/stereo/6ch 及 2/3/4 段重构，Float32 最大误差建议 ≤1e−6，旁路逐样本一致；单段压缩后在各段中心频率追踪实测 GR 与邻带泄漏。单段 +6 dB、本段未压缩时，单独听该段的幅度增加 6 ±0.1 dB。对新分频法做脉冲、延迟、尾音和不同块长。 [既有多段测试](../../tests/edit/test_bus_multiband.py) · [PDC 回归](../../tests/edit/test_clip_fx_pdc.py)

**首次修改前的自检。** 先让新声道数和原有双声道各跑同一组重构测试，再开放面板的 Band Count。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_bus_multiband.py tests/edit/test_clip_fx_pdc.py
```

**手动验收。** 叠加 80 Hz、1 kHz 和 8 kHz，分别调整三个频段；点击 Solo 时复核哪个频段听到，点击 Mute 切换后的成片要与实时一致。保存重开检查分频顺序和每段取值，历史含未生效 `makeup_db` 的项目再听一次存档。

<a id="fx-05"></a>

### FX-05 总线压缩器（P1）

**现状。** `BusCompEffect` 只接受胶合模板和 `sc_hpf_hz`，音频委托给 `CompressorEffect`，二阶高通只处理检源。历史模板写入的 `makeup_db` 没有进入底层 `makeup_gain`，不能作为同一版本的纠错增益直接启用。面板的 SSL/API 项需要明确的拓扑模型、工程字段和响应证据。 [处理器与模板](../../src/features/edit/core/audio_effects/bus_multiband.py) · [基础压缩器](../../src/features/edit/core/audio_dsp.py) · [面板](../../src/features/mixer/effects/panels/bus_compressor.py)

| 参数 | 建议生产合同 |
| --- | --- |
| 模板与处理器 | `template` 是数值预设，`topology` 是检测架构。初版保留 `feedforward`，外部可见的额外拓扑在独立音色和常数验收后开放。 |
| 首版映射 | `threshold_db`（dBFS）、`ratio`、`attack_ms/release_ms`、`makeup_gain`（dB）、Knee/Mix（旧底层为 %）、`sc_hpf_hz`（Hz，0 关闭）。 |
| Auto 与固定释放 | `release_mode` 分固定和自适应，分离存储。建议固定为新默认，测得双时间常数曲线且能重复渲染后启用 Auto。 |
| 侧链 | 默认内检，外检用工程 track ID。检源经高通后才进入包络；总线输出的声道布局不随检源改变。试听检源开关按临时状态。 |
| 旧模板升级 | `dx_glue`/ `mx_glue` 保留读取。升级时把旧运行时解析值展开到新版，补偿默认沿用既有实际生效的 0 dB，允许用户单笔修改。 |

1. 复制两套已有模板的参考 PCM 和处理器生效字段。实现 `BusCompParams` 的新版本字段及富预设应用，`template` 改动一次写明确可见字段。先让面板控制已有压缩数学，再开发反馈式或 RMS 模型。
2. 在 `BusCompEffect.set_params()` 区分“模板更改”和“旋钮更改”。现有处理器只在模板变化时重新应用模板，切换 sample rate 也要测试当前模板重装；让 HPF `zi` 只在速率/通道/HPF 拓扑变化时重置。各用同一音频实例维护包络。
3. 外部 PCM 路径经 `BusCompAdapter` 转入 `EffectChain.sidechain_map()`，扩展 `EffectChain.process()` 对检源能力的分发，并使 Track/Bus/Master 和未来 Clip 走同套 taps。对外检复制故障与路由环采用 FX-03 的诊断策略。
4. 代理 `CompressorEffect.last_gr_db`、`last_detector_db` 和 in/out tap。侧链高通开启时输出应仍含低频基音，变化来自增益控制。定频合并测量值，UI 不推算虚拟 GR。
5. 建议遵循 `输入 → wet/dry 混合 → Makeup`，写清补偿作用于总输出。SSL/API 按需求决定拓扑而非以名字猜测电路；反馈模型需单独做稳定性、攻击/释放和电平曲线验收。 [AES 压缩检测与设计](https://eecs.qmul.ac.uk/~josh/documents/2012/GiannoulisMassbergReiss-dynamicrangecompression-JAES2012.pdf)

**实验脚本。** 稳态硬膝、振幅域并行混合的参考。释放、检源滤波和状态跨块由生产测试覆盖。

```python
from math import isclose, log10

def output_db(level_db: float, threshold_db: float, ratio: float,
              mix: float, makeup_db: float) -> float:
    over = max(0.0, level_db - threshold_db)
    gain_db = -over * (1.0 - 1.0 / ratio)
    parallel_gain = (1.0 - mix) + mix * 10 ** (gain_db / 20.0)
    return level_db + 20.0 * log10(parallel_gain) + makeup_db

assert isclose(output_db(-10.0, -20.0, 2.0, 1.0, 0.0), -15.0, abs_tol=1e-10)
assert isclose(output_db(-10.0, -20.0, 2.0, 0.0, 0.0), -10.0, abs_tol=1e-10)
half = output_db(-10.0, -20.0, 2.0, 0.5, 0.0)
assert -12.5 < half < -10.0
assert isclose(output_db(-10.0, -20.0, 2.0, 0.5, 6.0) - half, 6.0)
```

**自动验收。** 硬膝 `−10 dBFS` 输入、阈值 `−20`、2:1、100% wet 得 `−15 ±0.2 dBFS`。做 0/50/100% Mix 的线性振幅比对。以 50 Hz 触发/1 kHz 持续信号证明开启 120 Hz 高通使低频触发减少、输出未被直接高通；检源静音/删除后恢复内检。实测独立总线与旁路轨的 PDC，再对拍相同 stem 和成片。 [现有总线压缩测试](../../tests/edit/test_bus_multiband.py)

**首次修改前的自检。** 首次提交只动模板参数与原处理器连接。先跑模板/高通基线，另开新测试检查补偿值在新版确实生效。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_bus_multiband.py tests/shared/test_audio_mx04_mx07_mx08_engine.py
```

**手动验收。** 节拍立体声送音乐 Bus，轨道人声作外部触发。调整模板及 HPF，在表上读相应 GR，关闭外检监听后试听完整混音；存档重开检查用户旋钮没有被模板重写。

<a id="fx-06"></a>

### FX-06 齿音消除（P1）

**现状。** `DeEsserEffect` 已有内检带通、Q、阈值、最大衰减、Ratio、Attack/Release、Wide/Split、Listen。现有检测先将通道平均，也将同一 mono 检测带用于 Split 的各通道减法。新版应为反相或不同立体声内容保留各自带通信号；兼容模式仍使用旧处理路径。 [齿音消除处理器](../../src/features/edit/core/audio_effects/de_esser.py) · [面板](../../src/features/mixer/effects/panels/deesser.py)

| 参数 | 建议生产合同 |
| --- | --- |
| 旧字段 | `frequency` Hz、`q` 无量纲、`threshold_dbfs`、`max_reduction_db`、`ratio`、`attack_ms/release_ms`。默认 6500 Hz、Q 2、−32 dBFS、12 dB 上限、4:1、5/60 ms。 |
| 新增检测形状 | `detector_shape` 用 bandpass/high_shelf，默认 bandpass。High Shelf 的检源信号与 Split 的被处理带分别定义，避免把高架基线混进带外路径。 |
| 时间与幅度 | `lookahead_ms` 按 0/2/5 ms 验证，默认 0 以保留旧延迟；`output_gain_db` 默认 0。延迟以实测样本数声明给 PDC。 |
| 监控 | `listen` 为临时试听，团队需确定旧已保存 Listen 的兼容策略。处理器同时提供 detector dBFS、GR dB、输入/输出样本峰和有效延迟。 |
| 兼容 | 旧数据继续原 bandpass + 旧声道语义；显式升级才用新版多声道检源、路由/输出形状和前视。 |

1. 分离 detector（控制信号）和 split band（被处理信号）。每声道用相同系数但独立 SOS 状态，先各自测带功率再以最大电平或归一化能量联动，以避免 L/R 相反时抵消。Split 用 `original - channel_band*(1-gain)`，Wide 用 `original*gain`。
2. 将高架检源设计接在检源支路，注明 shelf gain 和默认形状；输出直接复用实际被处理带。参考现行 RMS 窗，验证时间更新语义。`set_params()` 当前可能每次重置检源，先把连续旋钮修改和拓扑重建分开，平滑系数变更。 [W3C Audio EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/)
3. 增加预分配的 lookahead 环形缓冲。依规定样本对齐方式作用增益，`reset_state()` 清缓冲、滤波器和包络；0 ms 路径严格旁路无额外延迟。倒带及片段结束用现有 PDC 和 drain 边界推进。
4. 在 processing 末级做输出增益并采时序快照；Listen 输出检源波形而不输出整流包络。使用 `DeEsserAdapter` 进 `EffectChain`，UI 仅消费实测检测线和 GR，定位旧版本时保留原始已存字段。
5. 更新生产字段映射和版本门控，按有效 fs 限制滤波点，44.1/48/96 kHz 都包含反相和一高一低的声道回归。 [适配器与 telemetry](../../src/features/edit/core/audio_effects/adapters.py)

**实验脚本。** 固定衰减的 Split 参考，用相反声道证明各段不能先求 mono；此脚本只验证信号分解和稳态数学。

```python
import numpy as np
from scipy.signal import butter, sosfilt

fs = 48_000
t = np.arange(fs) / fs
x = 0.2 * np.sin(2 * np.pi * 1000 * t) + 0.2 * np.sin(2 * np.pi * 6500 * t)
stereo = np.stack([x, -x])
sos = butter(2, [5000, 8000], btype="bandpass", fs=fs, output="sos")
band = sosfilt(sos, stereo, axis=-1)
y = stereo - band * (1.0 - 0.5)

def amplitude(signal: np.ndarray, hz: int) -> float:
    tail = slice(fs // 4, None)
    oscillator = np.exp(-2j * np.pi * hz * t[tail])
    return float(2 * abs(np.mean(signal[tail] * oscillator)))

def change_db(hz: int) -> float:
    return float(20 * np.log10(amplitude(y[0], hz) / amplitude(x, hz)))

assert abs(change_db(1000)) < 0.15
assert -6.3 < change_db(6500) < -5.7
np.testing.assert_allclose(y[0], -y[1], atol=1e-14)
```

**自动验收。** 合成 6.5 kHz 齿音叠 1 kHz 参考音。验证达到设定 GR、Split 低频变化 ≤0.5 dB、Wide 使两段同降、Listen 显示实测检源。反相案例再做分块、seek、关闭 lookahead（实测 0）以及 2/5 ms 48 kHz 脉冲（96/240 个样本）。 [现有齿音测试](../../tests/edit/test_de_esser.py) · [Clip FX PDC 回归](../../tests/edit/test_clip_fx_pdc.py)

**首次修改前的自检。** 先为已有分段行为建基线，再在新版运行反相和小块长矩阵。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_de_esser.py tests/edit/test_clip_fx_pdc.py
```

**手动验收。** 在对白寻找干净元音与长 s/sh，扫检测频点并开/关 Listen。使用相同输出增益切 Wide/Split 对拍，听低频位置是否稳定；导出内检版本，再在片段边界核对听感与实时 GR。

<a id="fx-07"></a>

### FX-07 去点击／去嗡声（De-Click / De-Hum）

**目标与当前入口。** 第一版作为片段编辑器中的选区修复，原长保留、结果可试听和撤销。已有 `detect_clicks`、`remove_clicks`、`detect_hum_base`、`remove_hum`，输入平面 PCM `float32[channels, samples]`。`remove_hum` 用前后向滤波，适合离线。`_interpolate_repair` 的实际实现调用 `np.interp`。左右上下文的坐标会在常见窗宽下重叠，宜用斜坡夹点击测试固化正确的坐标。 [离线 DSP](../../src/features/edit/core/de_click_dehum.py) · [已有用例](../../tests/edit/test_de_click_dehum.py)

1. **建立可重放基线。** 用一段有可知原始 PCM 的信号叠加 50/60 Hz、各次谐波、1–12 样本点击，再加入完整的鼓击和爆破音。跑当前纯函数，保存误检、命中、修复窗口、频带衰减和指定声道结果。补 SciPy 缺失、前后向滤波填充长度不足、安静窗不足和反相声道。绝对时间索引用样本表达。
2. **先修检测与滤波边界。** 用 `ClickCandidate.position` 定位，将左右上下文放进同一绝对样本轴。候选重叠先合并，每段从本步骤开始前的输入取上下文。把 `width` 的半窗含义集中到一处。单独计算每声道的高通、宽带能量和置信度，再合并证据，防止反相立体声平均后抵消。无足够上下文时跳过并报原因。De-Hum 先比较安静窗频带显著性，自动模式仅在置信合格时处理。短选区借邻近上下文滤波，最后只写回目标区间。
3. **定义第一版参数。** 建议两个独立 Enable、选区 `[first_sample,end_sample)`、声道列表和版本。Click 保存 `click_threshold_sigma`（初始 8、建议 2–30）、`click_max_width_ms`（初始 2.5 ms，全窗定义）、`click_left_share_pct`（初始 50%）及策略 ID。Hum 保存 `hum_base_mode`（Auto/50/60）、`hum_harmonics`（1–16 的选择集合）、`hum_q`（初始 30）、`hum_drift_hz`（初始 0.6 Hz）、`hum_reduction_db`（0–48 dB）。Click Widening、Sensitivity、Skew 从原型百分比经查表或公式转换，逐个验证单调性。
4. **接到片段命令。** 在 `audio_edit.py` 的 `PROCESSORS` 注册长度不变的预渲染步骤，接入处理栈校验、`render_pre_render_steps` 和签名。通过 `ProjectClipEditor.audio_edit_committed` 提交一次变更。候选、PSD 与差分 `dry - wet` 由可取消任务返回，核对源、选区、声道、前序处理签名和 generation。差分及界面 Focus 仅影响试听。若需交付差分，另设显式输出模式。 [处理栈](../../src/features/edit/core/audio_edit.py) · [片段命令入口](../../src/features/mixer/ui/project_clip_editor.py)
5. **分开发布实时版本。** 如需轨级机架，另做因果 notch 与限长前视点击修复，声明版本、延迟、seek/flush 行为和支持的频率追踪速度。Welch、STFT 或读盘放到可取消任务。第一版离线步骤和单声道可验收后，再开放实时和多声道扩展。

| 提交次序 | 代码改动 | 验收样例 |
| --- | --- | --- |
| 1 | 停在纯函数层，修复坐标、候选合并和短窗滤波。为湿声削减定义有限衰减，例如将完全 notch 后与原声混合，使目标频点保留 `10 ** (-reduction_db / 20)` 的幅度。 | 斜坡六点污染、窗口贴左/贴右、两候选重叠、不同通道各有一次点击。 |
| 2 | 栈校验写入 `processor_id`、版本、修复范围和声道，缓存签名同时覆盖处理顺序、阈值、频点和源 revision。 | 替换前序步骤、更新缓冲、重开旧项目。移动选区时取消旧任务。 |
| 3 | 分开挂接 UI 分析、听检和提交按钮，取消只恢复当前选区的实际 PCM。 | 快速切换全声、差分、A/B。测两路同一延迟并允许相减回原输入。 |
| 4 | 将前后向滤波和因果滤波作为不同实现验收。 | 用脉冲核对相位和块接缝，低频漂移负例不触发自动修复。 |

**局部插值与已知嗡声的验证样例。** NumPy 实验检查修复窗坐标和频点测量。已知频率的正弦投影为人工注入提供检测基准。生产路径按上面的滤波和瞬态保护实现。

```python
import numpy as np

reference = np.linspace(-0.15, 0.15, 100, dtype=np.float64)
pcm = reference.copy()
start, end = 43, 49
pcm[start:end] += 0.8
indices = np.r_[start - 6:start, end:end + 6]
result = pcm.copy()
result[start:end] = np.interp(np.arange(start, end), indices, pcm[indices])
np.testing.assert_allclose(result, reference, atol=1e-12)
np.testing.assert_array_equal(result[:start], pcm[:start])
np.testing.assert_array_equal(result[end:], pcm[end:])

rate = 8_000
time = np.arange(rate * 2) / rate
hum = np.column_stack((np.sin(2 * np.pi * 50 * time),
                       np.cos(2 * np.pi * 50 * time)))
clean = 0.2 * np.sin(2 * np.pi * 220 * time)
dirty = clean + hum @ np.array([0.08, -0.02])
estimate = np.linalg.lstsq(hum, dirty, rcond=None)[0]
np.testing.assert_allclose(dirty - hum @ estimate, clean, atol=1e-12)
```

**验收。**

- 用 `.venv/bin/python -m pytest tests/edit/test_de_click_dehum.py -q` 跑已有基线，再补斜坡、短窗和宽带保护。清单中 10 个已知点击至少命中 9 个，定位误差 ≤2 样本。保护的鼓击及爆破音在固定素材基线零误修。50/60 Hz 稳态处降幅建议 ≥20 dB，220 Hz 对照损失 ≤0.5 dB。
- 将选区外和未选声道与原始输入逐样本比较。轨锁、快速改选区、试听途中关闭和取消不提交命令。手动试一次 Apply、撤销、重做、保存重开，并用 `render_audio_edit_full` 与实际 Stem 抽样对拍。
- 共振器输入测试无可信频率时输出逐样本不变，报告「未检测到稳定嗡声」。强制 50/60 模式则由用户明确选择。长区段检查任务内存随批次预算变化，当前工程的旧基底可回退。

<a id="fx-08"></a>

### FX-08 音高校正（Pitch Correction）

**目标与当前入口。** 先做 Clip 级、单声部有声音源的固定时长自动校正，再做图形编辑和实时插槽。新版面板已有 `get_state`、键盘和 `graphAnchorsChanged`。现有 `audio_timepitch.pitch_shift` 能对一段 PCM 应用固定半音值并恢复长度。变声器的自相关 F0 估计用于计量，可对照其范围和置信策略。 [面板](../../src/features/mixer/effects/panels/pitch_correction.py) · [变调函数](../../src/features/edit/core/audio_timepitch.py) · [变声处理](../../src/features/edit/core/audio_effects/voice_changer.py)

1. **做可取消的单声部分析任务。** 输入选中声道、绝对源样本窗、源指纹、前序处理签名、采样率和版本。初始建议 65–1000 Hz、50 ms 窗、10 ms hop，低声部增加窗长。用 YIN 或已验证的周期估计输出 `{sample_center, f0_hz, confidence, voiced}` 帧列。对静音和清音标记无有效 F0，保留原音。比较相邻候选，限制不合理跳变，回看八度错误。多声道先确定参考声道，核验反相素材。 [YIN 原论文](https://www.ee.columbia.edu/~dpwe/e6820/papers/deChevK02-yin.pdf)
2. **定义自动模式。** 将连续 F0 换算为浮点 MIDI，从启用的 12 音集合挑选目标，`bypass_notes` 使当前音名直通。音距相等先保留上一个目标，再用固定的低音优先决胜。持续有声帧经门限滞回合并为音符。在 cents 上按 Retune 的 ms 时间常数平滑，短音和长音分别调 Flex/Humanize，再引入颤音保留。无启用音、显式旁路及置信不足时校正量为 0。建议 `ref_a4_hz` 400–480 Hz 默认 440，`retune_ms` 0–400 默认 45，Strength/Mix 0–100%，Transpose −24…+24 st。
3. **接上保时长合成。** 首个可听里程碑只对完整稳定音符施加固定偏移，短重叠渐变回原样并检查等长，再迭代逐 hop 平滑变调和共振峰保持。静音及清辅音走等延迟直通路径。实时版需要有界历史窗、声道共享控制量、声道独立缓冲、延迟声明、seek 重置和尾部 flush。
4. **再接图形模式与持久化。** 保存锚点 `id/source_start_sample/source_duration_samples/midi/offset_cents/drift` 和源采样率。界面的 beat 通过片段速率和时间映射转换，源样本域字段为工程依据。定义连续锚点插值、重叠或越界拒绝、移位/修剪/拆分/倒放后的重算范围。长分析数据用可重建缓存或项目资源 ID 引用，缓存按输入签名失效。保存 `pitch_correction` 算法及 schema 版本，通过可撤销片段步骤提交。开放轨级调音时再定义无固定源锚点的实时语义。 [处理栈](../../src/features/edit/core/audio_edit.py) · [片段试听和提交](../../src/features/mixer/ui/project_clip_editor.py) · [现有变调测试](../../tests/edit/test_audio_timepitch.py)

| 提交次序 | 输入和输出 | 验收样例 |
| --- | --- | --- |
| 1 | PCM 切片与窗口参数进分析任务，输出有声帧轨迹和来源签名。Analysis 输入的采样率同时写入时间映射。 | 220/443/880 Hz、交替音符、反相双声道、静音、白噪声和贴近窗口边缘的短音。 |
| 2 | 基频轨迹、启用音、绕过音、调性和速度参数进校正曲线，输出同源样本轴的 cents 轨迹。 | 固定 A4 先校正到 440 Hz，改到参考 442 Hz 后重新求值。验证旁路优先级以及等距决胜。 |
| 3 | 校正曲线和 PCM 进保时长合成，输出相同形状的修复 PCM 和过渡记录。 | 栈顺序改变后旧缓冲失效，清音不调，边界淡化不出现尖峰。 |
| 4 | 手修锚点进入独立撤销事务，预渲染读取提交快照。实时版与预渲染分别声明精度、延迟和尾音。 | 片段首尾、拆分折点、1.5× 变速及保存重开时，锚点误差不超过一个时间域转换样本。 |

**F0 与量化独立验证。** 下面用等长差分窗、累积均值归一化和阈值选取形成小型 YIN 基线，用于可控合成信号回归。生产补分帧缓冲、多候选、音符追踪和边界处理。

```python
import math
import numpy as np

def f0_demo(frame, rate):
    lag_min, lag_max = rate // 1000, rate // 65
    size = len(frame) - lag_max
    if size <= 0 or np.sqrt(np.mean(frame ** 2)) < 1e-4:
        return None
    diff = np.array([np.sum((frame[:size] - frame[lag:lag + size]) ** 2)
                     for lag in range(1, lag_max + 1)])
    score = np.r_[1.0, diff * np.arange(1, lag_max + 1)
                  / np.maximum(np.cumsum(diff), 1e-12)]
    for lag in range(lag_min, lag_max):
        if score[lag] < 0.15 and score[lag - 1] >= score[lag] <= score[lag + 1]:
            left, mid, right = score[lag - 1:lag + 2]
            correction = 0.5 * (left - right) / max(left - 2 * mid + right, 1e-12)
            return rate / (lag + correction)
    return None

rate = 48_000
time = np.arange(2_400) / rate
f0 = f0_demo(0.2 * np.sin(2 * np.pi * 443 * time), rate)
assert f0 is not None and abs(f0 - 443) < 0.5
assert f0_demo(np.zeros_like(time), rate) is None
midi = 69 + 12 * math.log2(f0 / 440.0)
allowed = [note for note in range(128) if note % 12 in {0, 2, 4, 5, 7, 9, 11}]
target = min(allowed, key=lambda note: (abs(note - midi), note))
cents = 100 * (target - midi)
assert target == 69 and -13 < cents < -10

# 将已测得的偏移应用到一整段稳定音符，再独立测量输出。
from src.features.edit.core.audio_timepitch import pitch_shift

note_time = np.arange(2 * rate) / rate
note = (0.2 * np.sin(2 * np.pi * 443 * note_time))[None, :].astype(np.float32)
corrected = pitch_shift(note, rate, semitones=cents / 100)
steady = corrected[0, rate // 2:3 * rate // 2]
peak_hz = int(np.argmax(np.abs(np.fft.rfft(steady * np.hanning(steady.size)))))
assert corrected.shape == note.shape
assert abs(peak_hz - 440) <= 1
```

**验收。** 在 44.1/48/96 kHz 跑 A3、升高 30 cents 的 A4、慢速滑音、带 ±40 cents 颤音及白噪声。稳定有声音校正后中段误差目标 ≤5 cents，输入/输出样本数一致，静音绝对峰值 ≤1e−7，固定清音段无校正。手动试调式、音符绕过、图形锚点移动、切片后保存重开，再用监听/成片/Stem 核对同一音符位置和输出。开放实时前用输入脉冲检查旁路/湿声延迟一致到 1 样本。

<a id="fx-09"></a>

### FX-09 · 延迟 Delay

**现有能力。** `DelayParams` 存低切/高切（Hz）、左右时间（0–1200 ms）、有符号反馈（−99～99%）、高频衰减比、左右交叉比例和干湿比（%），以及输出增益（dB）。`link_lr` 只控制界面联动。四种输入选择为 Stereo、Mono、Left、Right。旧工程里的 LCR、Quad、5.0、5.1、7.1 会按 Stereo 读取。DSP 已有分数延迟、双读头交叉淡化及零反馈快路径。 [参数与旧格式归一化](../../src/features/edit/core/timeline_v2.py) · [DelayEffect](../../src/features/edit/core/audio_dsp.py) · [设计与图形](../../src/features/mixer/effects/panels/delay.py)

1. **先让回声图有可复核的数据。** 在 `DelayEffect` 参数变化后，发布当前采样率、左右读头位置（样本）、实际反馈、交叉比和状态版本。图的横坐标用 `1000 * delay_samples / sample_rate` 换算毫秒。第一跳及后续回声可在 worker 对参数快照打脉冲。图上分别标明单边输入、滤波衰减与反馈翻相，停播清表。时间拖动期间展示提交给双读头的目标时间，过渡中的实际抽头单独标记。
2. **先设计布局再扩展枚举。** 建议第一批为 Stereo 之外增加命名的前方 L/R 与环绕 Ls/Rs 配对；其余声道直通，LFE 默认隔离。使用 `ChannelLayout.channel_labels` 构建输入矩阵与反馈矩阵，禁止按偶/奇序号猜声道。把路由作为新字段，例如 `layout_mode`，默认沿用 Stereo 对旧项目的映射。遇到未支持的布局按明确诊断回退；多声道模式同版本中同步给预览和成片/Stem。 [声道布局合同](../../src/features/edit/core/channel_layout.py)
3. **保留块间状态与尾音。** 每实例维护读头、写位置、交叉滤波状态。参数更新在块边界交接，时间走现有读头过渡，反馈系数平滑；实时回调避免分配增大的整条延迟线。对有符号反馈使用绝对增益做稳定性及尾音预算。零反馈仍保留第一次回声；串联尾音累计。效果选择的音乐性延迟属于尾音和选区预卷，`latency_samples()` 只声明处理实现引入的公共固定延迟。Seek 清状态后按现有离线/实时约定恢复历史。

**可运行参考。** 假设固定整数延迟、全湿、无滤波。用反馈矩阵计算每一跳。这里用完整数组写得清楚，实时实现使用有界环形缓冲。

```python
import numpy as np

def fixed_delay(x: np.ndarray, samples: int, feedback: np.ndarray) -> np.ndarray:
    if samples < 1 or np.linalg.norm(feedback, ord=np.inf) >= 1.0:
        raise ValueError("delay must be positive and feedback must decay")
    y = np.zeros_like(x, dtype=np.float64)
    for n in range(samples, x.shape[-1]):
        y[:, n] = x[:, n - samples] + feedback @ y[:, n - samples]
    return y

fs = 48_000
delay = fs * 80 // 1000
x = np.zeros((2, 3 * delay + 1))
x[0, 0] = 1
# Left → Right gain +0.5, Right → Left gain -0.5.
y = fixed_delay(x, delay, np.array([[0, -0.5], [0.5, 0]]))
np.testing.assert_allclose(y[:, delay], [1, 0])
np.testing.assert_allclose(y[:, 2 * delay], [0, 0.5])
np.testing.assert_allclose(y[:, 3 * delay], [-0.25, 0])
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 时间与传播 | 固定时间送单边脉冲，在 44.1/48/96 kHz 逐跳读样本 | 整数读头首跳落于预期样本 ±1；带滤波的生产输出与相同配置下的参考包络对拍 |
| 自动 · 跨块和尾音 | 64/256/1024 样本分块，读头从 80 ms 拖到 160 ms；再串联两条 80 ms 无反馈 | 固定参数对拍目标最大误差 ≤1e-5；过渡无单样本冲击；串联第 160 ms 回声进入导出 |
| 手动 · 路由与兼容 | 5.1 逐声道脉冲；打开旧 Quad 字段的工程 | Ls/Rs 成对、C/LFE 保持指定策略。旧工程原路径声音保持，图与实测回声顺序一致 |

从现有分数延迟、时间交叉淡化和串联尾音测试开始。 [读头交叉淡化](../../tests/edit/test_audio_r07_delay_xfade.py) · [无反馈快路径](../../tests/edit/test_audio_r08_delay_hotpath.py) · [现有尾音验证](../../tests/edit/test_audio_review_followup_dsp.py)

<a id="fx-10"></a>

### FX-10 · 算法混响 Acoustic Reverb

**现有能力。** `ReverbEffect` 对输入做单声道湿声激励，按时序读出早期反射（ER），通过八线反馈延迟网络（FDN）产生晚期混响。ER 与 Late 各自着色，最后按 `dry_wet` 分别放入干、湿路径。H/L/W 和距离用米，预延迟和 RT60 用毫秒。现有模型允许负预延迟作为旧值读回，有效延时沿用当前钳制。项目保留 `ReverbParams` 的现有字段及默认。 [ReverbEffect / FDN](../../src/features/edit/core/audio_dsp.py) · [ReverbParams](../../src/features/edit/core/timeline_v2.py) · [响应图](../../src/features/mixer/effects/panels/reverb.py)

1. **拆分“抽头”和“实测”。** 保持当前房间几何和 ER 模型，从 `_compute_taps` 返回的抽头信息生成只读预测图，标注依据为模型及当前采样率。以参数全量快照在分析 worker 建立全新的 `ReverbEffect`，用单声道单位脉冲分别测 Direct、ER、Late、合成四种输出。生产引擎的环形和 FDN 状态不参与分析。
2. **让曲线表达可测量的量。** 以毫秒对齐直达零点。峰值包络找 ER 时间，晚期只在信噪比较好的区间拟合逆向积分能量斜率，例如 −5 至 −35 dB 外推到 60 dB。用一次图形计算同时返回时间轴、峰值桶、能量衰减、样本率和拟合区间。注明时长不足或只有干信号时无法测 RT60。拖动连续参数时合并重复请求，切换工程、效果实例或采样率时取消旧请求。
3. **验收分支后再做声道扩展。** 支持真实多声道湿声前，工程继续使用内部单声道激励、现行湿声分配逻辑并如实标记。若增加多输入或独立声道网络，新增算法版本及布局策略，保留旧版本渲染，并明确检测尾音与 FDN 消隐条件。监听、Clip audition、成片和 Stem 共用模型、预卷及时长预算。

**可运行参考。** 做一个指数衰减的理想 IR，用逆向积分和 −5 至 −35 dB 拟合校验 RT60 计算。例程只验证度量，不描述房间反射或多声道 FDN。

```python
import numpy as np

def measure_rt60(ir: np.ndarray, sample_rate: int) -> float:
    energy = np.cumsum(np.square(ir, dtype=np.float64)[::-1])[::-1]
    if energy.size == 0 or energy[0] <= 0:
        raise ValueError("no measurable decay")
    db = 10.0 * np.log10(np.maximum(energy / energy[0], 1e-20))
    time = np.arange(db.size) / sample_rate
    fit = (db <= -5.0) & (db >= -35.0)
    if np.count_nonzero(fit) < 2 or db[-1] > -35.0:
        raise ValueError("decay window is too short")
    slope, _ = np.polyfit(time[fit], db[fit], 1)
    return -60.0 / slope

rate = 48_000
nominal = 1.2
t = np.arange(4 * rate) / rate
ideal_ir = 10.0 ** (-3.0 * t / nominal)
assert abs(measure_rt60(ideal_ir, rate) - nominal) < 1e-4
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 分支 | 拨到全干且 Direct 0 dB；再 Wet 100% 独测 ER 和 Late，预延迟增加 20 ms | 干路逐样本一致；两条湿路起点按引擎抽头整体移动，48 kHz 差约 960 样本 |
| 自动 · 衰减 | 中性染色、无调制下，对 0.5/1/2/4 s 做中低频 T30 测量，再比较小/大块分割 | RT60 误差 ≤现有测试的 10%；有调制时块切换相位连续，静音尾部无自激 |
| 手动 · 图与输出 | 新开两个混响，快速扫预延迟后立即撤销并关闭其一 | 图标出预测和实测来源，旧回包丢弃。导出与播放在同一尾音预算下响至同一结束点 |

[现有 ER 几何回归](../../tests/edit/test_audio_rv02_er_model.py) · [FDN 衰减与稳定性](../../tests/edit/test_audio_rv03_fdn.py) · [实时与导出分块回归](../../tests/edit/test_audio_review_followup_dsp.py)

<a id="fx-11"></a>

### FX-11 · 卷积混响 Convolution Reverb

**现有能力及实测基线。** `ConvolutionReverbParams` 有 `ir_path`、`wet=0.35`、`dry=1.0`、`true_stereo=False` 和旁路。IR 解码支持 16/32-bit 整数 PCM WAV，经过重采样、裁首尾、短窗和峰值规范化。处理器能接收显式四路径矩阵；从 WAV 打开的现行入口未传矩阵。清空或加载失败会清除装载键，旧 kernel 仍可能留在实例。48 kHz 下用 `[1, .25]` 测到卷积输出从样本 0 开始，而 `PartitionedConvolver.latency` 报告 2048 样本；应先用真实输出对齐验证历史窗和 PDC 的区别。 [IR 读写、卷积和能力](../../src/features/edit/core/audio_effects/convolution_reverb.py) · [参数](../../src/features/edit/core/timeline_v2.py)

1. **P0 复现后拆出 worker 装载。** 建议 `set_params` 只消费规范化参数与准备完成的 kernel；worker 先检查 WAV 格式、声道标签、最大时长和读取上限，再按块解码、取消检查、同一字节流计算 SHA-256、重采样和建频域 kernel。缓存键至少包含内容哈希、目标率、声道语义和预处理版本。项目保存可重连的资源 ID、哈希和可移植定位信息；`IrResource` 承载解码信息。旧 `ir_path` 作为迁移读取入口，成功确认资源后由工程命令写入新引用。 [可复用项目重连入口](../../src/shared/project/offline_asset_manager.py) · [重采样方法](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)
2. **定义状态和原子交接。** 按工程、实例、generation 区分准备中、可用、取消、清空与失败。用户取消尚未提交的浏览时恢复原选择；对已确认的空引用或无效引用，在音频块边界停用旧 kernel、清历史、回报直通与可诊断状态。同一路径替换必须重新验哈希；重复预听不得在音频回调逐块读盘。输入/输出声道数量不同和无重采样能力时直接出诊断。对短 IR、干湿并存及混合其他轨的脉冲分别测 PDC；只报告真实输出新增的固定延迟。
3. **准备长 IR 的卷积计划。** 现有 convolver 的 FFT 长度会随 IR 全长增大，且在 `process` 中重算频域 IR。先在准备阶段冻结固定 kernel 的频域表示，再对长 IR 考虑统一大小分区、频域延迟线和 overlap-save。首分区的缓存策略决定实际算法延迟。用尾音长度及实测时延分别验收，压测 0.1/1/10 s IR，记录内存和最大小块处理耗时。
4. **P1 接真实 IR 内容。** 将四路 WAV 声道显式标为 `LL, LR, RL, RR`（第一字母输出），用 `set_ir(..., matrix=...)` 接入，缺路径给出策略并测试。裁剪/预延迟/窗函数/反向在目标率上按统一样本时间更新 kernel。建议先定反向与 trim 是否要交换操作顺序，确认尾音预算后再做 ER/尾部分离、宽度、内置库和扫频反卷积。面板图读准备完成的 IR 峰值桶、原始采样率与处理后长度。 [卷积面板](../../src/features/mixer/effects/panels/convolution_reverb.py)

**可运行参考。** 用直接卷积校验 four-path routing 的行列。该算法适合短测试，生产路径继续使用 FFT；尾音输出长度应包含全部因果样本。

```python
import numpy as np

def route_ir(x: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    return np.stack([
        np.convolve(x[0], kernel[d, 0]) + np.convolve(x[1], kernel[d, 1])
        for d in range(2)
    ])

h = np.array([[[1.0, 0.25], [0.5, 0.0]], [[-0.2, 0.1], [0.8, 0.4]]])
left = np.array([[1.0, 0.0], [0.0, 0.0]])
right = left[::-1].copy()
np.testing.assert_allclose(route_ir(left, h)[:, :2], h[:, 0, :])
np.testing.assert_allclose(route_ir(right, h)[:, :2], h[:, 1, :])

# 用同一套刺激信号检查生产 convolver 申报的时延。
from src.features.edit.core.audio_effects.convolution_reverb import PartitionedConvolver

processor = PartitionedConvolver(np.array([1.0, 0.25], dtype=np.float32))
pulse = np.zeros((1, 256), dtype=np.float32)
pulse[0, 0] = 1.0
response = processor.process(pulse)[0]
onset = int(np.flatnonzero(np.abs(response) > 1e-6)[0])
print({"measured_onset": onset, "reported_latency": processor.latency})
if onset == 0:
    np.testing.assert_allclose(response[:2], [1.0, 0.25], atol=1e-6)
# 修复版的测试将 onset == processor.latency 作为通过条件。
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 矩阵与延迟 | LL/LR/RL/RR 填不同系数，左右各打一次脉冲，覆盖长度仅 1 样本的双声道 IR | 矩阵顺序正确；目标误差 ≤现有卷积基线 1.5e-6；同采样率下 dry/wet 和 PDC 对齐至实测样本 |
| 自动 · 资源失败与替换 | 已加载 A 后清空、选择损坏 B、原路径写入不同内容、取消任务、切换工程 | 清空或确认失败后旧尾音归零；哈希改变后用新 kernel；取消/迟到任务不会替换当前状态 |
| 手动 · 移机交付 | 保存后关闭项目，移动媒体目录再重连，监听并导出同一脉冲 | 诊断解释缺失资源，复联后状态和波形正确。重新打开工程及 Stem/成片都指向同一版本 IR |

[现有卷积矩阵、解码与规范化回归](../../tests/edit/test_convolution_reverb.py) · [IR 路径生产入口验证](../../tests/edit/test_fx_rack_dialogs.py)

<a id="fx-12"></a>

### FX-12 · 空间声像 Spatial Panner

**现有能力。** 这是轨或 Bus 的 `EditTrack.pan` 与 `EditTrack.panner`，通过声像命令提交。轨平衡和自动化使用 `pan ∈ [-1,1]`，UI LR 使用 −100～100。现有 `SpatialPanner.process` 处理前两路并保留其余路，`spatial_panner_matrix` 计算 Spread、Rotation 和 Divergence 的立体声矩阵；FB 经 Auto Spread 间接影响立体声。Boom 使用 120 Hz 低通，依据 Pre/Post/Only 选择矩阵前后或仅低频的立体声近似。画面上的 5.1/7.1/7.1.4 和高度参数可保存，独立空间投影仍需实现。 [轨道参数](../../src/features/edit/core/timeline_v2.py) · [现有矩阵](../../src/features/edit/core/audio_dsp.py) · [声场面板](../../src/features/mixer/effects/panels/panner.py)

1. **先接闭环的平面单声道。** 基于 `ChannelLayout.channel_labels` 建目标扬声器水平角，左右、前后确定期望方位。第一批 5.1 使用 C=0°、L/R=±30°、Ls/Rs 为 ±100°～120° 范围内的工程选定角；LFE 不参加水平投影。立体声保持现行矩阵作为默认基线；5.1/7.1 模式取实际目标 Bus 布局，显示实际各声道名称与增益。7.1 用明确的侧面和后置角表，7.1.4 保持提示，直到有高度布局和渲染支持。 [5.1 声道参考](https://www.itu.int/rec/R-REC-BS.775-4-202212-I/en) · [布局合同](../../src/features/edit/core/channel_layout.py)
2. **处理顺序与兼容。** 在 `AudioMixer` 与 `AudioGraphRenderer.render_track_block` 共用纯矩阵生成接口。建议新增 panner 渲染模式，缺省为当前 Stereo 矩阵；使用新模式时保留源声道身份到投影阶段。避免先把 mono 放到 C 或做普通 pan 再进行空间投影导致重复换算。冻结 LR/FB 方位与旋转公式后再加入双声道宽度、Divergence、Spread 及角点平滑，布局切换时做块边界交接。
3. **单独验 Boom 路由。** 独立 LFE 只向命名 LFE 行发送。确认 Pre/Post 对立体声版本仍相对于矩阵前后；新增版本另定义相对于推子和空间矩阵的精确位置，在 UI 和交付输出中使用同一解释。明确 Only 对其余声道在新版本的静音策略，建立跨块低通状态及 seek 重置。提供带布局 ID 和实例时间戳的峰值帧供 UI/监看。

**可运行参考。** 给定包围目标方位的两个扬声器，用两个方向向量解出非负权重，再做平方和归一化。示例只验证一对扬声器；生产代码先处理角度回绕及退化扇区，拒绝 LFE 进入方向表。

```python
import numpy as np

def pair_gain(left_deg: float, right_deg: float, azimuth: float) -> np.ndarray:
    left, right, target = np.deg2rad([left_deg, right_deg, azimuth])
    speakers = np.array([[np.cos(left), np.cos(right)],
                         [np.sin(left), np.sin(right)]])
    gains = np.linalg.solve(speakers, [np.cos(target), np.sin(target)])
    if gains.min() < -1e-9:
        raise ValueError("target is outside the speaker pair")
    gains = np.maximum(gains, 0.0)
    return gains / np.linalg.norm(gains)

np.testing.assert_allclose(pair_gain(-30, 30, -30), [1, 0], atol=1e-12)
np.testing.assert_allclose(pair_gain(-30, 30, 0), [2**-0.5] * 2)
assert np.isclose(np.sum(pair_gain(30, 110, 65) ** 2), 1.0)
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 矩阵 | 在 5.1 的 C、L、Ls 朝向送 mono 脉冲，另测扇区中心 | 命中命名声道，LFE 静音；各方向增益平方和误差 ≤1e-6；old Stereo 仍通过原样本测试 |
| 自动 · LFE 和自动化 | 48 kHz 输入 60 Hz 和 1 kHz 双音，逐一切 Pre/Post/Only、seek、跨块 LR 曲线 | 低频与既定路由匹配；120 Hz 二阶低通稳态下 1 kHz 衰减目标 ≥30 dB；LR 手势、保存重开及导出采用同一真源 |
| 手动 · 实机 | 在 stereo 与环绕真实监看接线，逐声道 ID 报号并移动声像 | 电平表、扬声器声道、Stem 与成片轨匹配；高度模式清楚显示实际支持状态 |

[现有 Stereo panner](../../tests/edit/test_audio_dsp_pro.py) · [环绕 Bus 路由](../../tests/edit/test_surround_bus_routing.py) · [实时入口](../../src/features/edit/core/audio_playback.py) · [离线入口](../../src/features/edit/core/audio_graph_renderer.py)

<a id="fx-13"></a>

### FX-13 · M/S 立体声成像 Stereo Imager

**现有能力。** `ImagerParams` 存 `width`（0–200%，100 为原宽度）、`pan`（−100～100，0 为中位）和旁路。 `ImagerEffect` 做 M/S 分解、Side 宽度缩放和线性平衡。mono 只有现行 Pan 的平均衰减；多声道时前两路处理，其余保留。新 UI 的独立增益、Mono Maker、矢量和互相关度等有数据接入缺口。 [ImagerParams](../../src/features/edit/core/timeline_v2.py) · [ImagerEffect](../../src/features/edit/core/audio_dsp.py) · [示波面板](../../src/features/mixer/effects/panels/stereo_imager.py)

1. **P1 定义参数及信号顺序。** 建议保持原 `width` 和 `pan`，扩展 `mid_gain_db=0`、`side_gain_db=0`、`output_gain_db=0`、`mono_maker_on=False`、`mono_maker_hz` 和斜率枚举（默认值与面板核对）。先在允许目标声道对上做 M/S 分解，Mono Maker 衰减低频 Side，再应用 Width 和 M/S 增益，逆变换，最后 Pan/输出。旧参数与新默认组合应用旧快速路径，以原渲染作兼容基准。初版可保留 stereo 和显式前方 L/R 对；保留多声道其他未受影响声道，不推断编号。
2. **滤波及监听状态。** 为 Side 用实际截止频率、Q 和可选阶数设计 SOS，保存每实例每声道的跨块滤波初态；切频时平滑参数或交叉旧/新滤波输出，seek 重置。按 `Side - lowpass(Side)` 可得到与低路互补的参考，但任意阶数的低通相减并不自动对应面板标注的坡度，量产前用扫频核验；也可选取按目标斜率设计的高通。Mid/Side Solo 暂作可撤销试听覆盖，明确不随正式成片导出，旋钮与增益仍由工程命令持久化。
3. **接测量。** 从实际已选声道对发布同时间窗的去采样矢量点、M/S 峰值或 RMS、互相关系数。对每个实例定频、限点，保留原样本对，禁止两个声道独立抽样造成伪相关。静音/关窗/seek 清归零状态，关联项目及实例 ID。沿用 `update_metering` 参数入口并标注测量点是处理前或处理后。

**可运行参考。** 用一阶低通的互补残差验证 Mono Maker 只削 Side；工程使用其目标斜率与数值实现。此例的单音跨多个时间窗，可跳过滤波建立瞬态后测幅值。

```python
import numpy as np

def ms_pair(
    left: np.ndarray, right: np.ndarray, sample_rate: int, cutoff_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    mid = (left + right) * 0.5
    side = (left - right) * 0.5
    alpha = np.exp(-2.0 * np.pi * cutoff_hz / sample_rate)
    low = np.empty_like(side)
    state = 0.0
    for i, value in enumerate(side):
        state = (1.0 - alpha) * value + alpha * state
        low[i] = state
    high = side - low
    return mid + high, mid - high

fs = 48_000
t = np.arange(fs) / fs
signal = np.sin(2.0 * np.pi * 20.0 * t)
left, right = ms_pair(signal, signal, fs, 200.0)
np.testing.assert_allclose([left, right], [signal, signal])
left, right = ms_pair(signal, -signal, fs, 200.0)
assert np.max(np.abs(left[fs // 2:])) < 0.11
np.testing.assert_allclose(left + right, 0.0)
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · M/S 与兼容 | 纯 Mid、纯 Side、单边脉冲，Width=0/100/200，Mono Maker 关 | Side 分别为 0×/1×/2×，Mid 不变；旧实例与基线逐样本一致 |
| 自动 · 滤波与示波 | 开启 200 Hz，送 20 Hz 及 2 kHz 的纯 Side；测纯同相/反相和全静音 | 按采用滤波规格验收通/阻带及块连续性；相关度约 +1/−1，静音无有效相关度；窗口刷新无新增采样读取 |
| 手动 · 输出范围 | 5.1 中只开放显式 L/R 对，检查 C、LFE；执行 Side Solo 后导出 | C/LFE 逐样本保持既定策略；矢量图与实际 L/R 同时变化，正式导出使用已提交参数 |

[现有 Imager 基线](../../tests/edit/test_audio_dsp.py) · [声道标识](../../src/features/edit/core/channel_layout.py)

<a id="fx-14"></a>

### FX-14 饱和 Saturation

**现状与边界。** `SaturationEffect` 读取 `drive`（0–100）和 `enabled`。当前曲线为 `(1+k)x/(1+k|x|)`、`k=drive/2`，Drive 为零直通。新面板的全局 Drive 是 dB，且三段分别有模型、Drive、Tone、Bias 和 Level。旧 Drive 和它们没有一对一换算。 [现有处理器](../../src/features/edit/core/audio_dsp.py) · [现有模型](../../src/features/edit/core/timeline_v2.py)

| 顺序 | 具体落点与行为 |
| --- | --- |
| 1. 冻结旧声音 | 先为历史 Drive 0/20/80 保存 float PCM 基准。建议沿用 `saturation` 并增加受版本控制的 `model_profile=legacy\|multiband`，缺键走 `legacy`。扩展 `SaturationParams` 与其归一化函数。只有新建或用户明确升级才启用新模型。预设和项目保留原 Drive 与未知字段。 |
| 2. 先做单段 | 在 `audio_effects/` 建新处理单元，先做三个独立整形函数，路径为输入增益 → 有界整形 → 必要的 DC 阻断 → 输出 trim。可从对称 `tanh`、偏置 `tanh` 和 softsign 起步，分别校准小信号斜率、奇偶谐波比例和峰值。具体 Tape、Tube、Transistor 的曲线版本作为可更换设计件冻结。 |
| 3. 扩展三段 | 初版建议用互补分频实现可重构三段。验证 `0 < low_hz < high_hz < 0.45 * fs`，在采样率改变时重建系数。分别保存滤波状态、通道状态和波形整形模式。关闭某段的处理时保留该频段干声。Solo 属监听路由，与持久化处理旁路分开。总 Mix 用 0–1，项目中的百分数由 UI 映射，干声对齐湿声的实际延迟。 |
| 4. 抗混叠与热切换 | 对非线性路径做 2×/4×因果上/下采样，保留每个实例、每个通道的滤波历史和相位。建议首版在离线准备时选过采样率，播放中热切换后续用并行状态和短交叉淡化补齐。实测时延后回报 `latency_samples()`；需要滤波补偿的路径不直接相加。 |
| 5. 进阶与交互 | Auto Gain 先以稳态信号校准并限制补偿范围，再定义静音、seek 和旁路的复位条件。Flutter 接分数延迟和持续 LFO。Tape Stop 作为有绝对样本时间的动作，明确是否仅试听，若参与交付则保存触发时间和曲线版本。最后用 `classic_bindings.py` 接上已实际生效的字段。 |

**算法例与断言。** 以对称波形整形为例，用 10 kHz 正弦测量 3 次谐波折回到 18 kHz 的成分。此处整段多相采样用于离线对照。在生产流式实现中要单独管理边界和相位。 [SciPy 多相重采样](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html) · [SciPy 因果多相原语](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.upfirdn.html)

```python
import numpy as np
from scipy.signal import resample_poly

fs = 48_000
t = np.arange(2 * fs) / fs
x = 0.55 * np.sin(2 * np.pi * 10_000 * t)

def shape(signal, drive_db):
    drive = 10 ** (drive_db / 20)
    return np.tanh(signal * drive) / drive

def amplitude_at(signal, hz):
    steady = signal[fs // 2:3 * fs // 2]
    return 2 * abs(np.fft.rfft(steady)[hz]) / steady.size

direct = shape(x, 12)
oversampled = resample_poly(shape(resample_poly(x, 4, 1), 12), 1, 4)
assert oversampled.shape == x.shape
assert amplitude_at(oversampled, 18_000) < 0.1 * amplitude_at(direct, 18_000)
```

| 验证 | 输入、测量与通过条件 |
| --- | --- |
| 旧版与直通 | 用固定种子的浮点噪声和峰值大于 1 的输入测旧 Drive 三档。旧版与基线逐样本一致，旁路和零湿声保持原样，输入缓存未被修改。 |
| 分频与极限 | 用冲激和扫频检查各段和。各段处理全关、无延迟且同采样率时，float64 重构残差建议小于 `1e-10`。验证 NaN 参数、非法交叉、极端 Bias、静音和 DC。 |
| 抗混叠和补偿 | 自动比较 10 kHz 的 18 kHz 镜像，示例门槛为 4× 至少降低 20 dB。用两轨单位脉冲测开关过采样后的路径对齐，误差 ≤1 样本。运行整块与 127/513/1024 分块。 |
| 工程和耳听 | 打开旧片段、轨道及总线，切换模型、A/B 和旁路。应用后保存重开，撤销恢复旧模型与旧波形。对白、人声 S 音、鼓、低电平纯音用电平匹配的 A/B 检查刺耳折叠、明显 DC 或切换爆音。 |

起步测试见 [基础 DSP](../../tests/edit/test_audio_dsp.py) 和 [真实时间线效果导出](../../tests/edit/test_audio_dsp_timeline_export.py)。新增分频、过采样和历史 PCM 三类定向用例。

<a id="fx-15"></a>

### FX-15 激励器 Exciter

**现状与首版范围。** 面板已有 `exciter` 状态，生产参数注册表和 DSP 工厂目前缺此类型。先开放立体声、两段、每段可调 Amount/Mix 的版本，再扩展段数和风格。关键区别在于把新增的高频谐波并回干声，先定好幅度与带宽再开放自动补偿。 [设计字段](../../src/features/mixer/effects/parameter_schemas.json) · [处理器适配示例](../../src/features/edit/core/audio_effects/adapters.py)

| 顺序 | 具体落点与行为 |
| --- | --- |
| 1. 定合同 | 在 `timeline_v2.py` 建参数类型，建议 ID `exciter`，生产字段采用 `band_count`、`crossovers_hz`、`bands[]`、`oversampling`、`wet`、`output_gain_db`；每段 `enabled/amount/harmonics/bias/mix/output_gain_db`。比例统一 0–1，电平用 dB。首版试用两段、每段 Amount 默认 0，发布默认值前保证输出和关闭直通。Solo 仅监听，Mute 的渲染含义写入参数合同。 |
| 2. 提取谐波支路 | 共用 FX-04/FX-14 的分频和过采样基础。各段选择谐波模型，校准输入电平后进行有界整形。示例 Odd/Even 可分别启用奇次和偶次支路。用输出高通/SOS 消除偶次整形的 DC，回混原干声。每段先实现标量 Amount、Mix 和 Level，再用 `mode` 控制版本化的谐波权重。 |
| 3. 让流式行为可复验 | 实现每声道独立滤波/FIR 历史。增益及 Bias 参数变化平滑过渡。频带、分频和过采样档变化使用完整的新配置快照。共用 `EffectChain`，声明实际 latency，合成干湿的内部对齐和图级 PDC 分开验收。 |
| 4. 接工程和面板 | 首批从 Clip/Track 打通，Stereo Bus/Master 在性能及峰值验证后扩展。先完成注册和预检，再更新 Mixer 浏览器以及 `production_dialog.py` 的生产映射。尚未接通的段数和控制继续作为设计状态。为图形发布每段输入/输出和谐波残差的真实降采样 FFT 数据。 |
| 5. 扩展顺序 | 先补 3/4 段与预设，后做 Auto Gain。校准响度和峰值到两者各自的目标，使用有上下限的缓慢补偿。零 Amount 或全局 Mix 为零时保持干声直通，也保持同一切换合同。 |

**算法例与断言。** 平方支路的偶次谐波和 DC，经过高通保留测试中的二次谐波。输入为严格稳态小振幅正弦。生产实现额外做限带、过采样和状态持续。 [SciPy 二阶节滤波](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.sosfilt.html)

```python
import numpy as np
from scipy.signal import butter, sosfilt

fs = 48_000
t = np.arange(2 * fs) / fs
dry = 0.2 * np.sin(2 * np.pi * 2500 * t)
highpass = butter(2, 1200, "highpass", fs=fs, output="sos")
state = np.zeros((highpass.shape[0], 2))
harmonic_blocks = []
for start in range(0, dry.size, 513):
    block = dry[start:start + 513]
    harmonic, state = sosfilt(highpass, block ** 2, zi=state)
    harmonic_blocks.append(harmonic)
harmonic = np.concatenate(harmonic_blocks)
wet = dry + 0.5 * harmonic
steady = wet[fs:]
amplitude = 2 * np.abs(np.fft.rfft(steady)) / steady.size
assert abs(amplitude[2500] - 0.2) < 1e-5
assert 0.009 < amplitude[5000] < 0.011
assert abs(np.mean(steady)) < 1e-6
```

| 验证 | 输入、测量与通过条件 |
| --- | --- |
| 可控的谐波 | 2.5 kHz 信号按示例得到约 0.01 幅度的 5 kHz 分量。Odd 模式改测 7.5 kHz，频点与模型预定权重一致。DC 稳态低于 −100 dBFS，旁路与 Amount=0 精确干声。 |
| 混叠和增幅 | 用 10/16/19 kHz 小振幅正弦、对数扫频和满量程脉冲验证带边界，测 2×/4×的带内折叠。记录与 1×的改善量和过采样上限。静音与极低电平不被 Auto Gain 拉成可闻背景。 |
| 隔离和稳定 | 仅左声道输入，右通道无非预期串漏。全频带干湿相消核验对齐，分块、seek、参数跳变和重开项目后重复同一份信号。 |
| 用户验收 | 对录音齿音、钹和吉他分别做 0/30/70% 对比。使用旁路与低电平测试每段标记是否与声音一致。用 WAV 与 Stem 记录同一预设的定位误差和峰值。 |

注册和端到端验收可复用 [注册表健康测试](../../tests/edit/test_audio_effect_registry_health.py) 与[新效果多路径测试](../../tests/edit/test_audio_e2e_new_fx.py) 的结构。

<a id="fx-16"></a>

### FX-16 次谐波发生器 Subharmonic

**现状与首版范围。** 面板可编辑检测频率、带宽、下降一/两八度、二次整形和输出。生产中暂缺专用处理器。首版建议面向单一低频音源，例如贝斯或简单乐器，模型以置信度门控的生成支路实现。宽带鼓和混合素材的误触发属于验证对象。 [设计参数](../../src/features/mixer/effects/parameter_schemas.json) · [DSP 工厂](../../src/features/edit/core/audio_dsp.py)

| 顺序 | 具体落点与行为 |
| --- | --- |
| 1. 定义可重现的参数 | 注册 `subharmonic`，生产初版建议 `source_center_hz`、`source_bandwidth_hz`、`octave1_gain_db`、`octave2_gain_db`、`sub_lpf_hz`、`attack_ms`、`release_ms`、`mix` 和 `output_gain_db`。生成支路默认关闭。首版给检测器设 45–180 Hz 的目标范围，具体以素材测评确定。出界或检测不稳定时在释放时间内退回干声。 |
| 2. 检测和失锁 | 对链接立体声选取可用的检测信号，避免反相素材的 M 信号抵消后造成误判断。窄带加窗后计算归一化自相关，取第一个可信局部峰；低能量、相关峰低、频率突跳或缺足够周期时拒绝。使用固定 hop、进入/退出不同门限和最少持锁帧数；记录置信度、F0 和失锁原因。 |
| 3. 生成、合成与时延 | `phase += 2π f_sub/fs` 跨块累加，按锁定 F0 的 1/2 或 1/4 生成。目标频率和声部包络平滑切换。先做稳定正弦，再加有界整形、相位、立体声宽度和驱动模式。首版可将生成支路按等幅单声道路由到 L/R，保留输入 stereo。设计滤波、检测窗的已知延迟以及 dry/wet 对齐策略；选择有分析前视的版本时量出 PDC。 |
| 4. Seek 和独奏 | 固定输入时钟、hop 起点和振荡器复位规则，导出按既定预卷或确定性检查点恢复跟踪器。Sub Solo 先使用监听状态，不写入离线处理结构。记录缺失采样、过低频率推到设备不可闻和声道不支持的诊断。 |
| 5. 发布顺序 | 先 Clip/Track 立体声、单音和纯低频素材，再开放音效设计、录音回放和交付。用一次实际预览/成片/Stem 对拍核对生成音的开始、相位、尾音及参数。扩展前独立衡量 CPU 峰值和 buffer 长度。 |

**算法例与断言。** 对固定分析窗估计周期，再按常量 F0 生成两个八度。该代码是整窗稳态对照。生产实现使用有界的检测窗和连续状态，时长不足或低置信度时释放生成支路。

```python
import numpy as np

def estimate_frequency(frame, fs, low=45.0, high=180.0):
    x = frame - np.mean(frame)
    if np.sqrt(np.mean(x * x)) < 0.01 or x.size < 3 * fs / low:
        return None
    first = int(np.ceil(fs / high))
    last = int(fs / low)
    correlation = []
    for lag in range(first - 1, last + 2):
        a, b = x[:-lag], x[lag:]
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        correlation.append(np.dot(a, b) / max(norm, 1e-12))
    for index in range(1, len(correlation) - 1):
        left, center, right = correlation[index - 1:index + 2]
        if center > 0.8 and center > left and center >= right:
            return fs / (first - 1 + index)
    return None

fs = 48_000
t = np.arange(fs) / fs
f0 = estimate_frequency(0.3 * np.sin(2 * np.pi * 120 * t[:4800]), fs)
assert f0 == 120.0
assert estimate_frequency(np.zeros(4800), fs) is None
sub = 0.2 * np.sin(2 * np.pi * f0 / 2 * t) + 0.1 * np.sin(2 * np.pi * f0 / 4 * t)
amplitude = 2 * np.abs(np.fft.rfft(sub)) / sub.size
np.testing.assert_allclose(amplitude[[30, 60]], [0.1, 0.2], atol=1e-10)
```

| 验证 | 输入、测量与通过条件 |
| --- | --- |
| 输出频率 | 120 Hz 幅度 0.3 的单音输入，锁定后检测 60/30 Hz 两分量，稳定音高误差建议 ≤10 cents。用连续扫频跨过有效范围，检查失锁原因和生成支路平滑衰退。 |
| 错检与失锁 | 用固定随机种子的白噪声、低频为零的对白、鼓瞬态、反相立体声和静音。初版建议稳态静音 generated-only 峰值 ≤−80 dBFS，装配测试留出设计的释放时间。记录错检率，选出后来作为长期回归的代表信号。 |
| 立体声与版本 | 左右正弦分别做相同、反相和单侧三例。记录检测选择、干声极性和生成声道矩阵。参数变化用确定性淡化，seek 后按预卷验证两次相同导出结果。 |
| 手工走查 | 用耳机以等响电平切换 Sub Solo 和旁路，检查爆音、触发延迟、噪声误触发。保存和重开，再检查 Solo 监听状态和导出中的路由定义。 |

任务接入方式见 [混音链](../../src/features/edit/core/audio_dsp.py) · [成片与 Stem 对拍范例](../../tests/edit/test_audio_export_parity.py)。

<a id="fx-17"></a>

### FX-17 瞬态塑形（P1）

**现状。** `TransientDesignerEffect` 已实现快、慢两个包络及攻击/持续增益；包络的快慢比在 3–9 dB 之间构成软过渡。输入用声道平均的绝对值。新版只把实际现有的参数接到对应后端，新原型 Sensitivity、Punch/Smooth、Sidechain 和 Output Stage 图形还缺实际处理。 [现有瞬态处理](../../src/features/edit/core/audio_effects/modulation_tools.py) · [面板](../../src/features/mixer/effects/panels/transient_designer.py)

| 项目 | 建议生产合同 |
| --- | --- |
| 攻击/持续 | 保留旧 `attack_db/sustain_db` 工程存储，设计面板有符号百分比。推荐新版从 `db = 0.12 × percent` 起验，±100% 对应 ±12 dB，0% 对应 0 dB。 |
| 时序与灵敏度 | `attack_ms/sustain_ms` 旧默认 8/80 ms。新版 Punch/Smooth 用明确的快慢常数配置，`sensitivity_pct` 映射包络比的起振阈值。`punch_pct` 控制突出部分，0% 为中性。 |
| 干湿与侧链 | `mix` 归一化 0–1，Sidechain HPF/LPF Hz、斜率 dB/oct、Q 无量纲。内检关闭滤波仍继续检测。Listen 默认关、临时状态。 |
| 输出与版本 | `output_gain_db` 默认 0；软/硬输出保护独立旁路，额外 Profile 标记归一化 drive、clip 模式和 ceiling（样本峰 dBFS）。旧实例及其 dB 音色不改。 |
| 立体声 | 先支持独立或全联动，再按定义插值 `stereo_link_pct`。检测层避免直接混合相反极性的声道。 |

1. 查看现有 `TransientDesignerParams`，建立新旧前端单位映射。先明确 Attack/Sustain 的增益限幅和盘口舍入，旧数值超出百分比刻度时仍可显示取值并保持工程实际 dB。
2. 新版保留快、慢每声道状态，先实现无滤波的双包络、两时间常数和线性干湿。规定 `fast/slow` 的数值地板，弱信号绝对阈值防止停带时将噪声当瞬态。当反相声道共享检测时，按每声道绝对值求最大值再驱动。
3. 加入检源 HPF/LPF，使用各声道独立 SOS 状态。检源侧链只改变包络，启用 Listen 才听检测音频。将 Scope 发出的快、慢包络、瞬态权重、应用增益、峰值和限幅命中数标注实际时间轴。
4. 再进行输出阶段，建议 `检测 → 瞬态处理 → dry/wet → 输出补偿 → output clip`。Auto Gain 若增加，明确其是否使用预分析、是否要匹配稳态/响度，以及应用时的限幅重检。软波形和硬裁剪需标注样本峰，正式真峰交付走 FX-23。
5. 把当前版本区分的处理器或分支置于 `TransientDesignerAdapter` 之后。保持实例状态跨块。拓扑和滤波系数更改与音频线程内应用分开，关窗清遥测订阅。 [适配器](../../src/features/edit/core/audio_effects/adapters.py)

**实验脚本。** 验证前沿有提升，稳定段回到原增益，块边界状态正确传递。检测用给定幅度序列，实际应从可配置声道包络得到。

```python
import numpy as np

def weight(level: np.ndarray, state: tuple[float, float],
           fs: int = 48_000) -> tuple[np.ndarray, tuple[float, float]]:
    fast, slow = state
    a = np.exp(-1 / (fs * 0.008))
    b = np.exp(-1 / (fs * 0.080))
    output = np.empty_like(level)
    for i, value in enumerate(level):
        fast = a * fast + (1 - a) * value
        slow = b * slow + (1 - b) * value
        relative_db = 20 * np.log10(max(fast, 1e-9) / max(slow, 1e-9))
        output[i] = np.clip((relative_db - 3.0) / 6.0, 0.0, 1.0)
    return output, (fast, slow)

x = np.r_[np.zeros(60), np.full(48_000, 0.15)]
whole, _ = weight(x, (0.0, 0.0))
first, carry = weight(x[:643], (0.0, 0.0))
second, _ = weight(x[643:], carry)
np.testing.assert_array_equal(np.r_[first, second], whole)
assert whole[60] > 0.95 and whole[-1] == 0.0
gain = 1 + (10 ** (6 / 20) - 1) * whole
assert gain[60] > 1.9 and gain[-1] == 1.0
```

**自动验收。** 早期 10 ms 攻击峰提升接近设定 6 dB，稳定 1 秒后恢复 0 dB（各 ±0.2 dB 门槛）。Sustain 打负值后稳定段按给定幅度衰减。测试 Mix 0 且输出段旁路为逐样本直通，±反相左右、读空块和不同采样率不出 NaN。侧链筛掉鼓低频时核对 detector 和直接输出频谱。 [瞬态处理与调制回归](../../tests/edit/test_modulation_tools.py)

**首次修改前的自检。** 第一次只改百分比映射和混合开度。已有块连续与瞬态对拍保持通过后，再加 Filter 模块。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_modulation_tools.py tests/edit/test_clip_fx_new_effects.py
```

**手动验收。** 准备鼓点与长弦列，先提高 Attack、降低 Sustain，检查初始冲击与尾部是否分别变化。将画面时间定位到鼓点开头，验证实际检测过程和曲线同相。切软/硬输出，测波形峰和主输出是否符合选择；播放、seek、导出重复同一动作。

<a id="fx-18"></a>

### FX-18 · 合唱 / 镶边 / 移相 Modulation Trio

**现有能力与已复现偏差。** 一个面板分别编辑生产插槽 `chorus_flanger`（Chorus 或 Flanger）或 `phaser`。Chorus/Flanger 存 Hz、基准和调制延时（ms）、声部数、反馈与 Mix（归一比）；Phaser 存中心 Hz、Sweep（octaves）和级数。后者参数可读 1–12 级，DSP 目前到 4 级。Chorus/Flanger 在 48 kHz、5 ms 固定延迟、全湿、无反馈、单声部时首跳实测 2160 样本，目标是 240；读索引从线首计而写入在线尾。Phaser 带反馈的同一脉冲，在样本 501 切块后与整块最大差约 0.154；反馈样本当前为块内临时变量。 [生产 DSP](../../src/features/edit/core/audio_effects/modulation_tools.py) · [参数](../../src/features/edit/core/timeline_v2.py) · [面板](../../src/features/mixer/effects/panels/modulation.py)

1. **先用时域回归修读头和块状态。** 新算法版本使用写游标减去期望延时确定分数采样位置；预分配每个输入声道的环形缓冲，移除逐样本整线 `np.roll`。用 `base_ms + depth_ms * sin(phase)` 驱动每声部，前置定义可用最小/最大延时和超范围合法性。声部相位间隔和左右偏移保留跨块；Chorus/Tone、Flanger 反馈滤波及负反馈各自明确位置。版本化保留旧声音，或通过用户确认的升级命令赋予新的时域行为。
2. **定义 Flanger Thru-Zero 和 Phaser 状态。** 普通 Flanger 保持 dry 无固定延迟；Thru-Zero 版本在 dry 参考路径加入固定延迟并据实声明 PDC，wet 读头可跨越该参考时刻，跨点平滑。对 Phaser 设置稳定的一阶全通系数、每通道每级的历史输入/输出与跨块反馈；处理 2/4/6/8 级，兼容保留旧实例实际 1–4 级行为。调整级数应交叉淡出旧状态；立体声和多声道先声明策略再改。反馈尾音使用 `abs(feedback)` 和目标 dB，保留首跳，即使 feedback=0；老式预算中的 `-6` 与 “−60 dB” 注释不一致，迁移前加导出尾部测试。
3. **最后合并模块。** 新一体实例定义启用位、串联顺序或并行权重、延迟相对齐，参数按模块分树；现有两个 ID 继续各自读写和自动化。百分比深度在新版本按公开标尺映射到 ms 或 octaves，保留旧工程物理单位。计量从真实 LFO 相位、输入/输出与离线频率响应得出。 [参数绑定](../../src/features/mixer/effects/rack_bindings.py)

**可运行参考。** 前半段用有因果意义的整数读头叠加产生 Flanger 梳状抵消；后半段验证全通级联加反馈在分块处继承状态。仅用于验算固定参数；生产版加入可扫频的分数读头及独立通道。

```python
import numpy as np

def comb_reference(x: np.ndarray, delay_samples: int) -> np.ndarray:
    y = x.copy() * 0.5
    y[delay_samples:] += 0.5 * x[:-delay_samples]
    return y

def allpass_block(x: np.ndarray, stage_state: np.ndarray, feedback_sample: float):
    y = np.zeros_like(x)
    a, feedback = -0.4, 0.3
    for i, value in enumerate(x):
        value += feedback * feedback_sample
        for stage in range(stage_state.shape[0]):
            prev_x, prev_y = stage_state[stage]
            output = a * value + prev_x - a * prev_y
            stage_state[stage] = value, output
            value = output
        y[i] = feedback_sample = value
    return y, feedback_sample

pulse = np.zeros(480)
pulse[0] = 1.0
assert np.flatnonzero(comb_reference(pulse, 240)).tolist() == [0, 240]
x = np.zeros(1000)
x[500] = 1.0
full, _ = allpass_block(x, np.zeros((4, 2)), 0.0)
state = np.zeros((4, 2))
a, feedback_sample = allpass_block(x[:501], state, 0.0)
b, _ = allpass_block(x[501:], state, feedback_sample)
np.testing.assert_array_equal(full, np.concatenate([a, b]))
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 时间、状态和尾音 | 5 ms 固定镶边；48 kHz 每声道脉冲；两种不同分块，测试末尾正/负反馈 | 首跳为 240±1 样本；固定状态相同情况下块间最大差目标 ≤1e-5；末次回声在预算内衰减至规定阈值 |
| 自动 · 模式与频响 | 生成干湿各 50% 固定整数延时，测相邻梳状凹口；再测 Phaser 6/8 级冲激 | 首个凹口接近 1/(2D) Hz（D 为秒），相邻相隔 1/D Hz；Phaser 全通模块单独幅频近 0 dB，混合后出现凹口 |
| 手动 · UI、seek 与交付 | 保持旧工程的两个独立插槽，试听改 Voicing/绕过零点、暂停跳切再导出 | 旧项目留原处理，升级项目按新策略输出；实际波形、图与出片对齐，参数拖动无爆音；固定采样率的离线评估一致 |

[调制域既有回归](../../tests/edit/test_modulation_tools.py)

<a id="fx-19"></a>

### FX-19 · 颤音 / 自动声像 Tremolo & Auto-Pan

**现有能力。** `TremoloAutoPanEffect` 用跨块正弦相位，`mode` 为 `tremolo` 或 `auto_pan`，支持 Hz 与归一化深度 0–1。`TremoloAutoPanParams.shape` 有存储但当前 DSP 未消费；`Dual`、同步、波形选择和宽度等仍待接。旧 Auto-Pan 在深度 0 的两路增益各为 `sqrt(0.5)`；单声道输入会走 Tremolo 分支。新默认保留已存行为，以新算法版本调整增益或单声道语义。 [现有 DSP](../../src/features/edit/core/audio_effects/modulation_tools.py) · [参数](../../src/features/edit/core/timeline_v2.py) · [波形和分拍定义](../../src/features/mixer/effects/panels/tremolo_autopan.py)

1. **先做可测量波形。** 提取与 UI 状态对齐的参数，建议 `waveform` 单独存 `sine/triangle/square/saw_up/saw_down/random`；保留旧 `shape="sine"` 读法，新字段另定义占空比或曲线形状和单位。三角、方波、锯齿的数值输出可先以纯函数核对；方波/锯齿平滑使用规定的上升/下降时间与低通带宽，避免骤变。随机模式按 seed 与完整周期索引独立计算 Sample & Hold 或平滑曲线，保证离线分块与 seek 可重建。
2. **明确节奏时间真源。** 自由模式对每个样本积分 `Hz / sample_rate`；同步模式以 `BPM / 60 / beats_per_cycle` 得到 Hz，参照面板的附点和三连音定义。`retrigger` 明确是 transport 开始重置还是与工程拍网格锚定，切换、seek、循环与离线预卷采用同一时间源。建议先兑现手动 BPM 和拍值，再接项目的节拍变化来源；不存在工程 BPM 合同时继续手动模式并显示来源。
3. **再接 Dual 与表计。** 建议定义 Dual 为幅度乘法后再施加立体声声像，立体声相位差按度，中心和宽度按 UI 百分比换算。Tween 只影响声像或幅度所对应的模块；输出增益用 dB，定位为末端。逐块更新参数时保持 LFO 累计周期和光滑状态；有自由相位时说明 seek 可恢复范围。将真实相位和活动模式发给 `update_phase`，用测量的通道峰值填表。发布 Clip/Track/Bus/Master 同一版本的工程序列、采样率转换与自动化白名单。

**可运行参考。** 输入每样本速率，积分得到当前相位。该示例用前半段 2 Hz、后半段 3 Hz，验证参数切换时分块还有一条相位连续的时间轴；采用现有等功率声像的测试口径。

```python
import numpy as np

def lfo_block(rate_hz: np.ndarray, fs: int, phase_cycles: float):
    increments = rate_hz / fs
    phase = phase_cycles + np.concatenate(([0.0], np.cumsum(increments)))
    return np.sin(2.0 * np.pi * phase[:-1]), float(phase[-1])

fs = 48_000
rates = np.repeat([2.0, 3.0], [12_000, 24_000])
full, _ = lfo_block(rates, fs, 0.0)
a, phase = lfo_block(rates[:12_000], fs, 0.0)
b, _ = lfo_block(rates[12_000:], fs, phase)
np.testing.assert_allclose(full, np.concatenate([a, b]), atol=1e-10)
assert np.isclose(phase, 0.5)

pan = 0.7 * full
left, right = np.sqrt((1.0 - pan) / 2.0), np.sqrt((1.0 + pan) / 2.0)
np.testing.assert_allclose(left**2 + right**2, 1.0, atol=1e-12)
```

| 验收 | 操作 | 放行条件 |
| --- | --- | --- |
| 自动 · 速率/增益 | 120 BPM 四分拍，以稳定正弦载波测包络。切八分拍、方波占空 25% 和平滑 | 四分拍 2 Hz、八分拍 4 Hz；稳态占空符合设置，容差一个采样加平滑过渡。Tremolo 深度 0 直通；Auto-Pan 旧版本维持既有中心增益 |
| 自动 · 生命周期 | 120→180 BPM 或自由 2→3 Hz，在参数变化点切块；Random 同 seed seek 到相同周期 | 相位连续，往返/分块误差目标 ≤1e-5；随机周期状态、预卷及导出重新运行可复现 |
| 手动 · 真实立体声 | 左右送不同测试音，拖动相位与 Pan center/width，切 Dual 与绕过，试听/导出再看波形 | 左右调制和临近拍点正确，R/L 不串；表计反映实际相位/电平，状态栏标注自由、同步及 BPM 来源 |

[现有 Tremolo 和 Auto-Pan 测试](../../tests/edit/test_modulation_tools.py)

<a id="fx-20"></a>

### FX-20 加倍／和声（Doubler / Harmonizer）

**目标与当前入口。** 新版 `StudioDoublerWidget` 已有声部编辑、缩放视图、预设及 A/B。现有 `audio_timepitch.pitch_shift` 可用于离线固定移调，变声器的 `_PitchModule` 含流式有界缓冲、平滑和反混叠的参考实现。第一版做片段预渲染的固定音程双声，验收后扩到四声、调式和机架。 [面板](../../src/features/mixer/effects/panels/doubler.py) · [离线变调](../../src/features/edit/core/audio_timepitch.py) · [流式处理参考](../../src/features/edit/core/audio_effects/voice_changer.py)

1. **规定原始与效果声部。** 每声部保存稳定 `voice_id`、Enable、`interval_st`（建议 −24…+24 st）、`detune_cents`（建议 ±100）、`delay_ms`（0–100）、`pan_pct`（−100…+100）、`gain_db`（建议 −60…+6）、Mute。全局保存 1–4 声、`dry_gain_db`、`wet_mix_pct`、`stereo_width_pct`、`output_gain_db`、算法版本。示例双声默认左右错开 12/19 ms、Detune −6/+6 cents，保持 Dry 0 dB。初始化工程实例时可选择透明默认，界面演示预设由用户显式打开。Solo 定义为本机监听，写入交付需有独立输出选项。
2. **把单声处理闭环。** 先按 `interval_st + detune_cents / 100` 调一次现有保时长变调，统一修整到目标长度并处理尾端。用 `round(delay_ms * rate / 1000)` 做创作延迟，用等功率 Pan 分配到 L/R，再应用 dB 增益。各声部 Pitch 处理延迟走 PDC，对齐后仍保留各自创作延迟。采用每声部状态和固定输出峰值预算，允许浮点内部超过满幅，并保留实际峰值供下游控制。试听按显式的 Solo/Mute 选择声部。
3. **调式和人性化分阶段。** 从 FX-08 复用已验证的单声部 F0 轨迹。调式参数明确 `root`、`scale_notes`、音阶级进和固定半音两种音程单位。置信不足时保持固定和声或者直通，行为由模式确定。Humanize 通过 `random_seed`、`voice_id`、源绝对样本位置决定偏移，避免与回调块大小或声部增删顺序绑定。共振峰保持独立评估，避免声部之间共享会串扰的缓冲或随机序列。
4. **接项目与实时回调。** 片段版在 `audio_edit.py` 注册长度保持的预渲染步骤，定义选区外延迟声部是短渐出、仅剩片段内尾巴还是进入统一混音尾音。实时版为每声部准备有界延迟线和变调状态，控制线程发布新快照，渲染线程应用。按工程约定实现 `reset_state`、延迟/尾音、旁路淡变与能力注册；多声部状态和量化轨迹留在运行时，工程保存参数、seed 和必要资源引用。 [处理栈](../../src/features/edit/core/audio_edit.py) · [效果注册](../../src/features/edit/core/effect_registry.py) · [链工厂](../../src/features/edit/core/audio_dsp.py)

| 实现区块 | 第一版约束 | 回归证据 |
| --- | --- | --- |
| 参数校验 | 固定移调加微调后的总偏移须落在 `audio_timepitch.SUPPORTED_SEMITONES`。输入范围外给出可理解的参数错误。 | +24 st 再加 +100 cents 可诊断，−24/+24 st 恰在边缘可生成。 |
| 声道域 | 片段版须保留 PCM 的声道宽度。单声道项目输出单声道和声或显式转为已支持 stereo 轨布局，宽化仅在适用时开放。 | 硬左和硬右输出单声道部分无串扰，折叠策略在单、双声道一致。 |
| 声部叠加 | 采用固定参照的单声部增益和全局湿声增益，增删一个声音时其余声部不重新归一化。 | 各声部单独导出求和，对拍全湿输出。两个相同声部叠加的峰值读数可超过常规满幅。 |
| 路由状态 | Solo 是不写项目的试听状态，Mute 和 Enable 影响交付。Bypass 与 Cancel 还原整段混音，切换时保留湿声淡出预算。 | 保存重开只恢复已提交 Mute 和 Enable。监听 Solo 与 Stem 对拍。 |
| 动态扩展 | 固定声部先交付，再开放调式切换、Formant Preserve 和 Humanize 调制。每项用单独参数版本和听感测试。 | 对白男/女声、同调不同辅音、长元音和静音间隙评审。固定 seed 下左右控制相互独立。 |

**固定八度、延时和声像样例。** 在仓库根目录用 `.venv/bin/python` 运行，使用已有的离线变调和一段已知频率的信号。纯 NumPy 声像公式的断言独立于处理器实现。

```python
import math
import numpy as np
from src.features.edit.core.audio_timepitch import pitch_shift

rate = 48_000
time = np.arange(rate * 2) / rate
dry = (0.2 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)[None, :]
voice = pitch_shift(dry, rate, semitones=12.0)[0]
steady = voice[rate // 2:rate + rate // 2]
bins = np.fft.rfftfreq(len(steady), 1 / rate)
peak = bins[np.abs(np.fft.rfft(steady * np.hanning(len(steady)))).argmax()]
assert abs(peak - 440.0) <= 2.0

delay = round(15.0 * rate / 1000)
test_pulse = np.r_[1.0, np.zeros(31)]
post_delay = np.pad(test_pulse, (delay, 0))
pan = 0.4
angle = (pan + 1.0) * math.pi / 4.0
left, right = np.cos(angle) * post_delay, np.sin(angle) * post_delay
assert np.flatnonzero(right)[0] == 720
assert np.isclose(np.sum(left ** 2 + right ** 2), 1.0)
assert np.max(np.abs(right)) > np.max(np.abs(left))
```

**验收。** 固定测试音输入 220 Hz，+12 st 主峰在 440 Hz ±2 Hz，±6 cents 和声的频差误差目标 ≤2 cents（用频率拟合或足够长的测量窗）。44.1/48/96 kHz 下脉冲测创作延迟误差 ≤1 样本，加上 PDC 后干声仍落在原轨位置。明确规定尾音保留后测最后一个样本。手动试 A/B、改声部数、硬左/右、独奏、静音、保存重开、同 seed 复现、换 seed 明显可辨。实时版上线前比对整块与 127/512/1024 样本切块、seek 和 Stem 的冻结测试信号，稳定区 RMS 差目标 ≤1e−5 FS。

首轮离线可复用 `test_audio_timepitch.py` 的输入和已知基频测试。实时路径可参考 `test_audio_h05_pitch_stream.py` 的有界历史、Glide 和延迟测试，再补跨声部状态隔离。 [离线基础测试](../../tests/edit/test_audio_timepitch.py) · [实时变调基础测试](../../tests/edit/test_audio_h05_pitch_stream.py)

<a id="fx-21"></a>

### FX-21 变声（Voice Changer）

**目标与当前入口。** Formant → Pitch → Vocoder → Output Gain 已由 `VoiceChangerEffect.process` 消费，`VoiceChangerParams` 持久化控制参数和随机种子，`EffectChain` 负责建链。先恢复可信的真实计量并覆盖参数切换，音色扩展留在可版本化的下一批。处理器已有 `collect_telemetry`，报告 F0、置信度、进/出 RMS、模块开关和已处理样本数。 [DSP](../../src/features/edit/core/audio_effects/voice_changer.py) · [参数](../../src/features/edit/core/timeline_v2.py) · [UI 映射](../../src/features/mixer/effects/classic_bindings.py)

1. **保留现有参数语义。** 现有字段用于兼容。移调 `pitch_semitones` 按半音，`pitch_fine_cents` 按音分，`pitch_glide_ms`、`vocoder_release_ms` 按毫秒，混合、共振峰保留、Jitter 按既有百分比，输出 `output_gain_db` 按 dB。对 `formant_mode`、`pitch_tracking`、`vocoder_carrier` 沿用当前枚举。复用原始 seed 和历史版本以维护旧工程。以后增加新载波/共振峰路径时再引入版本和迁移。
2. **先把进出计量信号定义清楚。** 当前输入 F0 在前两声道里按能量挑参考，输出 F0 固定分析左声道且走默认范围。输入和输出 RMS 也固定读取左声道。为输入/输出各返回所分析的实际声道、测量窗和 F0 范围，用平面 PCM 的逐声道 RMS 和绝对峰值显示表计。输入和输出可共享参考声道身份，静音输出显示无读数。把 Vocal/Wide 对应的搜索范围和置信标记明确给图。未补偿的输出位置 `n` 对应输入 `n - latency_samples()`，计量发布前对齐样本位置，Dry/Wet 的补偿沿用 `_DelayLine`。
3. **连接管理层和窗口。** 在 `EffectChain` 的实例粒度采集当前遥测，跨块积累约 30–50 ms 的窗，以 `sequence/clip_or_track/effect_instance/sample_position/generation` 发布有界快照。完整窗口每秒读约 15–30 次。短块与无音频时保留统一的时间基准。禁止窗口直接读取可变 DSP 状态或复制整段音频。将面板演示 F0 和电平替换成真实数据，测不到时保留明确的空指示。模块级别的电平、载波频谱和共振峰曲线按需采集。
4. **核对重配置和尾音。** 有效版本和设备采样率改变时，重建 Formant/Pitch/Vocoder 并把当前 `random_seed` 重施到新模块。检查现有 `_rebuild` 后的 seed 缓存，避免外层缓存相同而跳过新模块初始化。检查 `latency_samples` 与各级实际窗口长度，工程 on/off、seek、取消 preview 应恢复延迟、RNG、分析窗和项目参数的一致语义。 [链装载](../../src/features/edit/core/audio_dsp.py) · [实时宿主](../../src/features/edit/core/audio_playback.py) · [既有尾部测试](../../tests/edit/test_audio_h03_tail_flush.py)

| 实施区块 | 首次提交 | 测试方法 |
| --- | --- | --- |
| 参数快照 | 通过现有版本与 schema 写真实参数，给旧预设补缺省，无效的枚举或字段保留旧值或按现有正规化处理。 | 人为删除若干旧字段重开，与旧算法参数快照对照。 |
| Formant | 确认 Natural/Crisp 的不同渲染效果。用已知峰的谐波源或滤波白噪声梳状输入估计移动后的包络位置。 | 0 shift 时核对时延补偿后的输入基线，分别检查左右声道和全部 Mix 模式。 |
| Pitch | 对固定 semitone、Fine、Glide、Link、Jitter 分开测量。公开每一模式实际所启用的 F0 测量范围。 | 按测试音频的中段估计音分误差，检查切块、调参数和 Link 状态切换。 |
| Vocoder | 保持各载波独立的 RNG 流。进声为上两级输出。对 Unvoiced 保留频段与实际控制量做诊断。 | 四种载波、静音、激励脉冲、右有声左静音和 −6 dB 输出。 |
| 遥测与性能 | 只为打开窗口或有诊断订阅的实例更新重分析。冻结遥测参数并限频发布，必要时 FFT 在供给有界窗的 worker 计算。 | 清空项目和切实例不串表。设备音频周期内测 1/4/8 实例的峰值成本及丢块，界面关闭后停止额外分析。 |

**真实输出和独立表计算法样例。** 三个模块关闭，仅打开已存在的 −6.02 dB 输出衰减。向右声道单独送入有声输入，验证声道选择、电平和能量比例。样例调用当前处理器，RMS/参考声道求值是独立测量，供未来遥测对照。

```python
import numpy as np
from src.features.edit.core.audio_effects.voice_changer import VoiceChangerEffect

rate = 48_000
time = np.arange(rate // 10) / rate
right = 0.25 * np.sin(2 * np.pi * 440 * time)
pcm = np.stack((np.zeros_like(right), right)).astype(np.float32)
processor = VoiceChangerEffect(rate)
processor.set_params({
    "enabled": True, "formant_enabled": False, "pitch_enabled": False,
    "vocoder_enabled": False, "output_gain_db": 20 * np.log10(0.5),
})
output = processor.process(pcm)
power_by_channel = np.mean(output.astype(np.float64) ** 2, axis=1)
reference_channel = int(np.argmax(power_by_channel))
assert reference_channel == 1
np.testing.assert_allclose(np.sqrt(power_by_channel[1]), 0.125 / np.sqrt(2), atol=1e-7)
np.testing.assert_array_equal(output[0], pcm[0])
np.testing.assert_allclose(output[1], pcm[1] * 0.5, atol=1e-7)
```

**验收。** 先跑 `test_audio_h05_pitch_stream.py`、`test_audio_h06_param_validity.py`、`test_audio_post_integration_voice_regressions.py`。新增 L-only/R-only、反相立体声、原始 F0 为 880 Hz 的 Wide 模式、静音与限幅样例。建立稳态合成信号的 F0 误差 ≤20 cents、RMS 误差 ≤0.1 dB、静音通道峰值 ≤1e−7 的门槛。用同 seed 连续两次、seek 后补齐相同预卷、变更缓冲大小、44.1/48/96 kHz 重新播和成片/Stem 逐段复核。过载测试需给后级可读峰值，保存参数时保留上游浮点内容。

验收时用 `.venv/bin/python -m pytest tests/edit/test_audio_h05_pitch_stream.py tests/edit/test_audio_h06_param_validity.py tests/edit/test_audio_post_integration_voice_regressions.py -q` 跑基础测试，再补通过生产窗口驱动的实例生命周期用例。 [流式 Pitch](../../tests/edit/test_audio_h05_pitch_stream.py) · [计量基础测试](../../tests/edit/test_audio_h06_param_validity.py) · [复合变声回归](../../tests/edit/test_audio_post_integration_voice_regressions.py)

<a id="fx-22"></a>

### FX-22 频谱变形（Spectral Mangler）

**目标与当前入口。** Ring、Grain、Dist、串/并联和输出保护已由 `SpectralManglerEffect` 消费。Freeze 开关映射到 `grain_freeze` 并能停止写入颗粒历史环。底层已有 `take_freeze_capture`、`install_freeze_capture`、`FreezeCaptureStore` 和资产装载接口。优先完成可存盘、可重放、可诊断的 Freeze 流程，再接颗粒和峰值显示。 [DSP](../../src/features/edit/core/audio_effects/spectral_mangler.py) · [Freeze 资产存储](../../src/features/edit/core/freeze_capture_store.py) · [现有回归](../../tests/edit/test_audio_h09_freeze_assets.py)

1. **定义可听参数和资源边界。** 保留现有路由枚举和 DSP 参数。建议清单明确 Ring Carrier 用 Hz、Grain Size 用 ms、Density 用 grains/s、Pitch 用半音、Jitter/Position/Spray/Feedback 用百分比、Dist Drive/Output 用 dB。保留当前 `random_seed`。Freeze PCM 独立存盘，参数中只写 `freeze_capture_id`。继续把资源 ID、随机种子和模式枚举从连续自动化排除。全局预设可记参数，资源 ID 仅属于单个项目。
2. **建立一次可撤销的捕获事务。** Freeze Toggle 若已有真实音频，发送带实例 ID、工程 revision、预览源和样本锚点的请求。渲染线程在块边界快照有界历史环，文件线程用现有 store 写暂存并校验元数据、样本数、声道顺序和校验和，成功后发布唯一 ID。控制器用一次命令更新开关与引用。接入已有 `EffectChain.install_freeze_capture_by_id`。关窗、换项目或取消后拒绝过期回包并收回无引用临时资产。撤销时保留能被重做及其他实例引用的资产。
3. **核对环形坐标和采样率。** 捕获环回卷后，保存绝对锚点和有效样本数，或将环按时间线性化并保存起点。现有 `install_capture` 会将锚点截在环容量以内，且未根据捕获 `sample_rate` 重采样。先补“写入长度大于一圈”及异率回放测试，再对超长锚点、换率、丢失文件和损坏元数据分别诊断。实时轨/Master、成片已有 registry 安装分支，沿 `audio_playback`、`timeline_export` 检查；片段 FX、独立片段试听及 Stem 另查可达性，按同一实例引用和时基补接。 [实时安装](../../src/features/edit/core/audio_playback.py) · [成片安装](../../src/features/edit/core/timeline_export.py) · [Stem](../../src/features/edit/core/stem_export.py)
4. **让表计对上实际数据。** `collect_telemetry` 当前包括模块开关、`frozen`、`clip`、`limiter_on` 和采样位置。增加捕获状态（等待、已安装、失败）、实际粒子数及丢弃计数、限制器前后绝对峰值。关闭限制器后也测当前峰值，避免沿用旧的 `last_clip`。路由切换的 5 ms 淡入、模块 bypass、全局 Mix 0/100% 与并联的延迟相位做脉冲对照。展示由当前实例发出的快照，过期数据丢弃。

| 实施区块 | 输入和输出 | 验收样例 |
| --- | --- | --- |
| 运行内捕获 | 框定由稳定实例消费的绝对样本区间，返回冻结数据和来源。临时停住历史写入期间，重复点击复用或取消当前作业。 | 空环时有原因提示。已有捕获的声音继续可回放。 |
| 工程资源 | 由项目资源表收集临时冻结件及已引用的 Asset ID。计算字节校验和，项目保存打包时使用相对引用。 | 替换同名目录、复制项目、资源缺失、破损校验、回收无引用资源。原 PCM 不进入效果参数 JSON。 |
| 样本变换 | 根据捕获历史率重新计算相对 anchor 和历史长度，必要时于离线加载或 worker 预处理。 | 44.1→48、48→96 kHz 保持冻结内容的实际时长和音高。移机及导出复用同版本重采样。 |
| 路由对拍 | 对串行各模块按累计时延对齐。并行先将各支路的 Wet−Dry 差量延迟到公共最长路径，再求和。 | 对 Mix 0、100 及三支路全开分别跑脉冲和固定长度随机输入。切换路由不重置仍在使用的种子或捕获。 |

**资产回放样例。** 在仓库根用 `.venv/bin/python` 运行。实际调用现有捕获、临时目录存储和重放接口；历史窗未超过一圈。后续另外添加回卷和异采样率两项回归。

```python
from tempfile import TemporaryDirectory
import numpy as np
from src.features.edit.core.audio_effects.spectral_mangler import SpectralManglerEffect
from src.features.edit.core.freeze_capture_store import FreezeCaptureStore

rate = 48_000
params = {"grain_enabled": True, "grain_density_hz": 30.0,
          "grain_mix_pct": 100.0, "global_mix_pct": 100.0,
          "random_seed": 77, "grain_freeze": False}
engine = SpectralManglerEffect(rate)
engine.set_params(params)
source = np.random.default_rng(11).standard_normal((2, rate)).astype(np.float32) * 0.1
engine.process(source)
captured = engine.take_freeze_capture()

def replay(asset):
    effect = SpectralManglerEffect(rate)
    effect.set_params({**params, "grain_freeze": True})
    assert effect.install_freeze_capture(asset)
    return effect.process(np.zeros((2, rate // 2), np.float32))

with TemporaryDirectory() as directory:
    store = FreezeCaptureStore(directory)
    assert store.save(captured)
    reopened = store.load(captured["id"])
    assert reopened is not None and reopened["anchor"] == captured["anchor"]
    first, second = replay(captured), replay(reopened)
    assert np.max(np.abs(first)) > 1e-4
    np.testing.assert_allclose(first, second, atol=1e-6)
```

**验收。** 先跑 `test_audio_h09_freeze_assets.py`、`test_audio_h07_dist_channels.py`、`test_audio_post_integration_mangler_regressions.py`。手动做录播素材 Freeze、换素材后二次 Freeze、取消、撤销重做、移机重开，做成片与 Stem 的指定起点导出进行录波对拍。缺失资产和失败的重采样应给诊断。同一输入、种子、预卷、参数和采样率的回放最大差建议 ≤1e−5 FS。全局 Mix 为零时与经 PDC 对齐后的 Dry 一致，关闭限制器仍有当前过载峰值读数。

<a id="fx-23"></a>

### FX-23 真峰限制器（P0/P1）

**现状。** 当前 `TruePeakLimiterEffect` 用 4× 过采样、前视、释放和链接/独立分支。需要先补读数和算法正确性。Link 分支目前平均左右再测峰，反相输入可能抵消；独立分支块间把包络折叠为单一最小值；前视 target 尾在短块下要验证尺寸与时间含义。现有 telemetry 的 `true_peak_dbtp` 来自检测支路，可另外提供实际输出峰。 [真峰处理器](../../src/features/edit/core/audio_effects/true_peak_limiter.py) · [真峰面板](../../src/features/mixer/effects/panels/true_peak_limiter.py)

| 项目 | 建议生产合同 |
| --- | --- |
| 旧有效值 | `ceiling_dbtp=-1`、`lookahead_ms=5`、`release_ms=80`、`linked=True`。保留旧 v 工程的算法分发与原历史渲染；修正版本显式升级并带诊断。 |
| 算法次序 | `输入 → DC filter（默认关）→ Gain/Trim（默认 0 dB）→ 前视和每声道真峰检测 → 限制增益 → 输出计量`。未经重新检测的正增益不要放在最后。 |
| 相位和时钟 | Link 取各声道峰值上界。Independent 每声道各有 release 历史。ms 转样本与 FIR 净时延之和由脉冲确认，声明给 PDC。 |
| LUFS 与目标 | UI 分别展示 Momentary/Short-term/Integrated、输入/输出 dBTP 与独立的 GR。Reset 只重建读数区间。Match 先异步分析，再预览并一次提交建议增益。 |
| Dither | 只对选择的整数字长，在交付最终量化前一次应用。保存明确位深、白噪/噪声塑形、seed 与是否静音空白；默认关闭。 |
| 审听 | Delta 必须对齐规定的延迟后求差；Unity 必须说明匹配策略；两项均属于试听并禁止改变成片参数。 |

1. 建立基线含 12 kHz 带相位纯音、反相双声道、极弱右声道伴强左、信号收尾极短块。先写反相和短块检测，确认现行链接/独立行为，再加新分支处理。暂停或 seek 后清理输入与输出的测量历史。
2. 按声道在一致的过采样相位测样本间峰，Link 对齐同一位置后求各声道最大。把历史 FIR 状态按采样率、通道数和过采样因子分配；如采用紧凑流式 FIR，跨块保留所需历史并以多个块长验证。前视用 O(N) 单调队列或等价持久环形窗，记录原始峰值序列与输出增益的位置。独立 Release 使用每声道状态数组。
3. 对过采样因子、真峰开关、Lookahead 更新准备新处理计划。更换时对齐干路和相邻轨，重建有效 PDC 后生效。验证暂停、seek、末块 drain，比较多轨脉冲对齐。有限 Attack 和自动释放必须一直遵守上限，Anti-clip 需要定义它保护的是样本峰还是真峰。
4. 用项目 `LoudnessMeter` 和 `analyze_loudness()` 做实际输出的响度测量。Match 用同帧范围/布局得到 Integrated，再计算 `target_lufs − measured_lufs` 的建议增益。叠加限制和输出计量复核，报告无法同时达到的动态交付目标。业务阈值从 `DeliveryPreset` 读取。 [响度分析器](../../src/features/edit/core/loudness_analyzer.py) · [交付预设](../../src/features/edit/core/delivery_preset.py) · [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I/en)
5. 在 `timeline_export.py` 与 `stem_export.py` 的整数输出入口接最后一次量化。先 TPDF，选统一归一化的 1 LSB 标尺并明确舍入与饱和；seed 随项目交付设置/声道确定。后续噪声塑形须校验滤波状态，浮点 WAV 关闭 Dither，避免导出两次叠加。 [AES Dither 论文](https://aes.org/publications/elibrary/elibrary-page/?id=5575)

**实验脚本。** 用 16× 离线插值作独立校验，忽略两端补零影响。前半段验逐通道链接峰值和固定增益余量，后半段验前视、即时衰减与缓慢释放。改变增益后要独立复测实际输出，正式计量以 BS.1770 验收为准。 [SciPy 多相重采样](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html)

```python
import numpy as np
from scipy.signal import resample_poly

def channel_peaks(x: np.ndarray, factor: int = 16) -> np.ndarray:
    up = resample_poly(x, factor, 1, axis=-1)
    edges = 64 * factor
    return np.max(np.abs(up[:, edges:-edges]), axis=1)

fs = 48_000
n = np.arange(fs)
tone = 0.98 * np.sin(2 * np.pi * 12_000 / fs * n + np.pi / 4)
audio = np.stack([tone, -tone])
sample_db = 20 * np.log10(np.max(np.abs(audio)))
input_peak = float(np.max(channel_peaks(audio)))
assert 20 * np.log10(input_peak) - sample_db > 2.9
assert np.max(np.abs(audio.mean(axis=0))) == 0.0

ceiling_dbtp = -1.0
ceiling = 10 ** (ceiling_dbtp / 20.0)
test_gain = min(1.0, ceiling / input_peak)
output_db = 20 * np.log10(np.max(channel_peaks(audio * test_gain)))
assert abs(output_db - ceiling_dbtp) < 1e-8

# 原速样本坐标上的前视增益对照。从完整的峰序列求未来窗口，
# 实时实现以流式队列提供同一窗口，同时延迟原 PCM。
from numpy.lib.stride_tricks import sliding_window_view

def lookahead_gain(peaks, ceiling, lookahead_samples, release_ms, fs):
    padded = np.pad(peaks, (0, lookahead_samples))
    upcoming_peaks = sliding_window_view(padded, lookahead_samples + 1).max(axis=-1)
    targets = np.minimum(1.0, ceiling / np.maximum(upcoming_peaks, 1e-12))
    release = np.exp(-1.0 / (release_ms * fs / 1000.0))
    gain = np.empty_like(targets)
    previous = 1.0
    for i, target in enumerate(targets):
        previous = min(target, release * previous + (1.0 - release) * target)
        gain[i] = previous
    return gain

peak_timeline = np.full(1_200, 0.25)
peak_timeline[500] = 1.2
gain = lookahead_gain(peak_timeline, 0.8, 240, 80.0, fs)
assert gain[259] == 1.0                        # 峰值尚在前视窗之外
assert np.isclose(gain[260], 0.8 / 1.2)        # 峰值进入前视窗
assert np.isclose(gain[500] * peak_timeline[500], 0.8)
assert gain[500] < gain[501] < 1.0             # 峰过后释放，连续回升
```

**自动验收。** 用 EBU 文件验证计量并以独立过采样复核新处理器输出；项目门槛可先设 ceiling +0.1 dBTP，最终以发布配置和官方测量公差为准。48 kHz、5 ms 当前前视夹具应量得 240 样本（若新版 FIR 有净延迟，更新合同和基线）。测 mono/stereo/5.1 与 31/257/1024 样本块，及独立模式的左右增益串动。Dither 测固定 seed、目标字长 LSB、无削顶溢出及 32-bit float 路径。 [EBU 官方测试集](https://tech.ebu.ch/publications/ebu_loudness_test_set) · [真峰回归](../../tests/edit/test_true_peak_limiter.py) · [片段 PDC 回归](../../tests/edit/test_clip_fx_pdc.py)

**首次修改前的自检。** 先跑现有真峰和对齐用例；新增独立表计夹具对照旧读数，再启用修正版实际上限限制。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_true_peak_limiter.py tests/edit/test_clip_fx_pdc.py
```

**手动验收。** 用 0 dBFS 附近的双声道反相测试音开启链接，观察 pre true peak 高于 sample peak，限制后输出低于目标。更改目标响度并分析一个完整片段，重置读数后重播。最终导出 16-bit、24-bit 与浮点，对拍各档采样率和节目 LUFS，保留测量报告。

<a id="fx-24"></a>

### FX-24 音频片段编辑器 Audio Clip Editor

**现状与边界。** 这是选区、时长与处理栈的编辑入口，正式工程中使用 `ProjectClipEditor`，波形、频谱、片段试听与机架已有生产链路。历史实测和随后已完成的项目分开记录，第七节给出当前状态。实施前按缺口复测，避免把报告前半部分的历史故障重新列为未做。 [片段编辑专项第七节](2026-11-21-mixer-clip-editor-backend-handoff.md#七复核修复与验收记录)

| 顺序 | 具体落点与行为 |
| --- | --- |
| 1. 固定一份工作坐标 | 入口解析 `resolve_clip_audio_spec` 得到源流和源区间。保存有理帧率、源/项目采样率、绑定/声道 patch 和方向。栈持久化使用 `audio_edit.processing_rate` 和整数半开区间。对原生变速、变长栈步骤和倒放分别应用既有映射，界面秒数只在边界转一次。做选区标准化时在步骤插入点测量，复用 `render_to_step_insertion`，并保留同范围手动增益操作身份。 |
| 2. 固定一条渲染路径 | 先执行 `render_prefix_steps` 预渲染，再请求 `suffix_audio_edit` 与包络，并依实际片段消费顺序应用 clip 音量、淡化、Clip FX。核对 EditPage、片段试听、成片和 Stem 对同一栈的实际顺序。若检测到顺序分歧，先写逐样本对拍，再合并到共享入口。使用现成的 `render_audio_edit_full` 做处理栈本身的离线对照。 |
| 3. 测试听和回包生命周期 | 维持 `ClipPreviewWorker.submit_cancellable`。worker 接收稳定的源指纹/patch、栈签名、采样率、FFT 和视区快照，计算期间在步骤和窗口边界检查取消。只有当前 generation 与请求键同时匹配时发布。显示最后一份有效 PCM 时标记新请求状态。比较 A/B 时按同一源事件位置切换，连续输出由 `clip_audition.py` 管理。 |
| 4. 收口编辑事务 | 静音、增益、降噪、频谱修复经既有处理栈入口提交；仅写目标通道。删选区、裁剪、变长经 `plan_clip_length_change` 及工程命令，统一链接 A/V、Sync Lock 和锁轨结果。试听 Solo Band、播放循环和显示模式保留为监听/界面状态。新去点击/去嗡声和图形音高校正在此复用相同的作业和命令边界。 |
| 5. 验证待交付边界 | 优先验证有交付能力的算法、新增效果叠加变速后的双向映射，以及不同真实设备的循环/跳选区与变长 A/B。切工程、锁轨或重新连接时，过期任务不能提交。保存影响输出的参数、源引用和版本；让 PCM、频谱图及临时文件由有界缓存重建。 |

**算法例与断言。** 在项目根目录运行。用实际处理栈验证仅左声道的增益、半开区间与绝对样本包络。在另一个测试中用同一思路为真实变速与多步栈构造脉冲输入。

```python
from fractions import Fraction

import numpy as np

from src.features.edit.core.audio_edit import (
    apply_clip_audio_edit,
    make_step,
    new_audio_edit,
    render_audio_edit_full,
)
from src.features.edit.core.sample_time import TimelineSampleClock

fs = 48_000
clock = TimelineSampleClock(Fraction(30_000, 1001), fs)
start = int(clock.frame_start_sample(12))
end = int(clock.frame_start_sample(15))
source = np.full((2, fs), 0.25, dtype=np.float32)
edit = new_audio_edit([make_step("range_gain", start, end, [0], gain_db=-6.0)])
whole = render_audio_edit_full(source, edit, sample_rate=fs)
blocks = [
    apply_clip_audio_edit(source[:, i:i + 733], i, edit, sample_rate=fs)
    for i in range(0, source.shape[-1], 733)
]
np.testing.assert_array_equal(whole, np.concatenate(blocks, axis=-1))
np.testing.assert_array_equal(whole[1], source[1])
np.testing.assert_array_equal(whole[0, end:], source[0, end:])
assert abs(whole[0, (start + end) // 2] - 0.25 * 10 ** (-6 / 20)) < 1e-7
```

| 验证 | 输入、测量与通过条件 |
| --- | --- |
| 样本和声道 | 以 48 kHz 处理栈选区作基准，44.1/48/96 kHz 复验 20 ms 分别对应 882/960/1920 个样本。左声道编辑后右及未选声道逐样本相同。含倒放、非整数帧率和 30 分钟远处切点。 |
| 中止与耗时 | A 请求人工延迟 300 ms，B 请求先完成，只接受 B。另测 A→B→A、失败重试和同键合并。运行 5 分钟立体声和长时录音，记录峰值 RAM、实体设备 underrun 与 UI 延迟。建议 UI 响应 P95 <50 ms，缓存按字节上限逐出。 |
| 工程和交付 | 对同一片段依次做增益、处理栈、淡化和至少一个有延迟的 Clip FX。保存、重开、撤销/重做、关闭窗口后比较实际 PCM。成片与无 Dither 的 24-bit Stem 按一个 LSB 量级核对。以明确的 marker 或脉冲对齐，分享样本基准与偏移量。 |
| 真机 | 分别测试系统默认输出和第二音频设备。用脉冲标记可辨认的跳过选区和循环边界，核对播放头、切设备和焦点变化。物理监听结果单独记录系统、设备、块长和采样率。 |

入口见 [片段 DSP 与编辑模型](../../src/features/edit/core/audio_edit.py) · [片段预览 worker](../../src/features/mixer/ui/clip_preview_worker.py) · [试听会话](../../src/features/mixer/ui/clip_audition.py) · [现有预览回归](../../tests/mixer/test_clip_editor_processed_preview.py) · [现有重复测量回归](../../tests/mixer/test_repeat_normalization_baseline.py)。

## 用于逐项交付的记录

每完成一款，在自己的验收记录里复制下表。状态建议填写「通过」「失败」「待实机」或「本批未开放」，同时写证据路径或复现命令。

| 记录项 | 填写内容 |
| --- | --- |
| 实例与作用域 | 效果器 ID、项目 schema / DSP 版本、支持的 Clip / Track / Bus / Master、实际声道布局。 |
| 工程兼容 | 老档和新档位置、未知键往返结果、旧音 PCM 摘要、两实例调序/撤销/重做。 |
| 核心算法 | 所用样本、测试命令、理论值、实测值、容差、整段与分块差异。 |
| 资源与生命周期 | 无设备、断链、坏资源、取消、关闭、清理、seek 与换采样率的结果。 |
| 时间、资源和听感 | 延迟/尾音样本数，块长和预算，P95/P99、峰值内存、设备和 A/B 结果。 |
| 交付对拍 | 浮点点位差、WAV 量化规则、成片及 Stem、测量器及版本、失败预检的提示。 |
| 面板能力 | 本批实际接通的参数、可用的计量、预设与自动化，留待下一批的项及理由。 |
