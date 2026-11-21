# 混音页面真实素材复测（2026-11-22）

使用 `tests/test_videos/2K_h264.mp4` 的原始 AAC 立体声音轨，验证生产面板参数、工程往返和实际混音输出。原素材没有修改。逐例量化数据和素材 SHA-256 见 [JSON 记录](2026-11-22-audio-mixer-real-media.json)。

## 本轮修复

1. **Mixer 打开媒体走错流程**：之前调用独立调色视频加载器，虽然状态栏显示成功，却没有导入当前工程媒体池。现在菜单使用媒体池的音频/视频多选入口，最近文件和路径打开使用同一异步导入流程。导入后仍由正常时间线编辑操作加入片段；没有自动改写用户时间线。
2. **媒体池文件筛选缺少通配符**：`.mp4` 等扩展名曾直接当成 Qt 文件匹配模式，目录中的正常媒体被隐藏。现在由共享格式注册表生成 `*.mp4`、`*.wav` 等模式；翻译只影响标签，不能覆盖格式列表。测试用实际 `QFileDialog` / `QFileSystemModel` 验证五类筛选和全部注册扩展名。
3. **机架显示内部 ID**：Exciter、Subharmonic 等新类型现在使用效果目录的本地化名称，机架行、提示和添加菜单保持一致。
4. **Master 的空间声像控件无处理通路**：原窗口可以调整，但 Master 没有对应的工程参数、信号连接和音频处理入口。现在禁用 Master 的 balance/空间声像控件及添加项，并防止程序调用创建无效窗口。普通音轨和 Bus 继续使用已有声像模型和 DSP；未新增 Master 工程 schema。

## 量化验证范围

- 素材：H.264 1920×1080 / 25 fps，AAC 48 kHz 双声道。取源第 125 帧（5 秒）起 1.2 秒，每块 1,920 samples，共 30 块；使用真实 `AudioDecoder` 解码，没有替换解码器或 DSP。
- **24 种插入类型 × Track / Bus / Master = 72 组**。每组从 `ProductionEffectDialog` 控件状态收集参数，经 Apply、`TrackEffectInstance` 序列化回读和 `EffectChainParams` 进入实际 `AudioMixer`。
- 每组验证：输出长度正确、无 NaN/Inf、非静音；启用处理后与干声存在实际差异；实例旁通与独立干声混音结果逐样本完全相等；未知参数字段保留，无 ERROR/CRITICAL 日志。
- 类型：Parametric EQ、Expander、旧 Noise Gate、Compressor、Limiter、组合 Dynamics、Reverb、Delay、Saturation、Imager、Voice Changer、Spectral Mangler、Exciter、Subharmonic、De-Esser、True Peak Limiter、Dynamic EQ、Bus Compressor、Multiband Compressor、Convolution Reverb、Transient Designer、Tremolo / Auto Pan、Chorus / Flanger、Phaser。
- 额外 **2 组空间声像**验证真实音轨和 Bus 的声道能量变化；**4 组离线工具**验证该 MP4 上的 De-Click、De-Hum、Pitch Correction、Doubler，检查未选声道/范围和输入样本精确保留。
- 卷积使用已知抽头的测试 WAV IR，实时异步准备完成后才处理。这验证加载/处理通路，不代表真实房间 IR 音质评测。
- 激励器须启用频段并增加 Amount；默认关闭全部频段时精确直通是既有合同，不能把仅调整输出旋钮当作激励支路测试。
- 生产机架与工程命令测试覆盖各类型窗口复用、删除清理、Apply、Cancel、旁通、undo/redo；主输出的无效声像入口另有回归。

新专项合并运行：**206 项通过**，耗时 77 秒。该数已包含上述 78 项真实素材检查，不重复累加。

扩大回归运行了 `tests/mixer` 全目录及媒体导入、播放、Bus、卷积和运行时相关用例：共 603 项，首次 601 项通过；两个菜单测试仍断言旧五类目录，缺少前轮已加入的 Exciter/Subharmonic 分类，另产生一次相同断言的 Qt teardown 错误。补齐真实目录预期并验证 Master 空间声像项禁用后，所在 `test_project_workspace.py` **45 项全部通过**（45.13 秒）。其余代码未再修改，无剩余已知失败。这里不将重叠的两次测试数量相加，也不声称第二次完整重跑了 603 项。

新增测试的 Ruff `F/E9`、相关 Python 文件编译和 `git diff --check` 通过。复现命令：

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/mixer/test_real_media_effects.py tests/mixer/test_production_effect_entrypoints.py tests/edit/test_media_pool_import_filters.py tests/app/test_gap007_file_controller.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/mixer tests/edit/test_media_pool_import_filters.py tests/app/test_gap007_file_controller.py tests/edit/test_audio_playback.py tests/edit/test_audio_fx_completion_runtime.py tests/edit/test_bus_multiband.py tests/edit/test_convolution_reverb.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/mixer/test_project_workspace.py -q
```

## 原生页面观察及限制

已在完整 Studio 应用中建立独立测试工程，进入实际 Mixer 工作区，复现原媒体打开流程错误；修复后通过菜单导入，媒体池 Master 中可见 `2K_h264.mp4`。也实际打开过空间声像窗口，并结合生产调用链确认 Master 控件无效。

**本轮没有完成每一种效果器在原生时间线上的逐窗播放、全部旋钮手势或物理声卡试听。** 随后当前会话的桌面控制接口被撤下，无法继续原生操作。上面的全类型结果来自生产 Qt 控件、工程命令与真实解码/混音器的自动化集成检查，不能当作主观音质、等响度 A/B 或硬件播放验收。

浏览器中 De-Click / De-Hum、Pitch Correction、Doubler 的设计预览仍有明确 pending 提示，不能拖入实时效果链；实际处理走片段选区离线工具。本次不把这些预览面板列为可用的实时插入效果器，也不改变原交付审计中记录的高级功能待办。

长时间录音、音频设备 underrun、第二台设备及 Windows 仍未验证。
