# Topos Studio 混音页后端接线计划表

Studio 的剪辑页和混音页共用同一个 Audio Mixer 面板，推子、双声道电平表、插入效果器以及 Master / Bus 输出路由均在同一面板中操作。混音页借用剪辑页的 Viewport 与时间线。Viewport 显示 Program，时间线以音频轨和按轨着色的波形呈现同一剪辑序列。音频轨道头的 R 复用录音控制器，成功录制后按播放头打点放入该轨。时间线音频片段和素材池音频文件均可通过右键进入混音页的同一个专业编辑器。素材模式读取实际源波形，片段模式的增益和帧级淡入淡出进入同一撤销历史。面板停靠布局按页面保存，工程编辑共用走带与音频遥测。独立原型入口仍可展示本地示例。

工作区借还入口见 [DockLayoutMixin](../../src/app/controllers/dock_layout_controller.py)，通道条外观见 [StudioAudioMixerPanel](../../src/features/mixer/ui/studio_console.py)。下表保留后端接线规划，其中原型专用的扩展参数与精调任务仍待实现。

| 顺序 | 后端工作 | 接线位置与边界 | 可验收结果 |
| --- | --- | --- | --- |
| 1 | 共享工程、序列与选区 | 在现有页面上下文和剪辑控制器上增加 Mixer 入口。将序列、轨道、片段的稳定 ID 投影到混音页。交互使用现有 `EditProject`、时间线命令栈及 revision，合并预览与最终提交。 | 剪辑页改名、移动片段或移动播放头后，混音页显示同一工程状态。跨页撤销一次只撤销一次用户操作。 |
| 2 | 媒体导入、重连与波形 | 让 `TimelinePanel.importRequested` 进入现有媒体池和导入流程。后台探测、解码和缓存每声道 min/max/RMS，按可见范围和缩放读取不同精度。以媒体 ID、版本、通道布局和范围标识缓存。 | 可导入三份测试素材。双声道波形、源偏移和裁剪边缘准确，导入失败可重试，切项目后不会采用旧任务结果。 |
| 3 | 时间基准和录播 | 使用 `TimelineSampleClock`、`AudioClock` 和现有播放服务替代 `ViewerPanel` 的走带计时。工程帧率及起始 TC 来自当前序列，统一 DF/NDF、帧↔采样换算与逐帧导航。帧磁吸和样本精度编辑各保留独立语义。接现有录音、输入设备、录音待命与监听。 | 23.976、24、25、29.97 DF/NDF 下长时间往返定位无累积漂移。循环边界、录音落点和播放头一致，切页面只使用一个播放会话。 |
| 4 | 视频监看 | 将 `PicturePreview` 接到已有节目监看及解码队列。共享音频时钟和预览请求的代际取消。浮动、合并、隐藏或切页时管理资源和订阅。 | 拖动播放头、逐帧或从剪辑页切进来时显示同一帧。空序列和离线媒体有明确状态。 |
| 5 | 通道条与路由 | 复用现有音频图、混音台命令和实时参数快照。绑定推子、静音、独奏、相位、输入 Trim、80 Hz HPF、声像、宽度、EQ、通道输出、Bus 与前后推子发送。路由保存稳定 Bus ID，验证循环、通道格式和删除后的去向。Master 的 MONO / DIM 作用于监听。 | 路由改名不会断开，非法反馈回路被拒绝，推子拖动可试听且一次撤销可恢复。MONO / DIM 不改变交付文件。 |
| 6 | 效果器与自动化 | 按 `EffectDescriptor.effect_id` 对照生产 Effect Catalog，核对参数 schema、默认值、版本、采样率及作用域。实现缺失的处理器，按真实能力开启预览和导出。旁路、顺序、混合量、延迟补偿、尾音和 sidechain 使用统一信号链。Read、Touch、Latch、Write、Off 绑定现有关键帧及手势提交。 | 同一效果在实时和离线路径参数一致。参数拖动不会阻塞音频线程。缺失插件或不兼容版本明确诊断。自动化可撤销和重开恢复。 |
| 7 | 单片段精调 | 接 `StudioAudioClipEditor.load_audio_data` 回填真实双声道波形、频谱与测量。将 gain、fade、trim、silence、envelope、markers、normalize、noise print、denoise、spectral repair、time/pitch 和 process stack 信号翻译为有序的非破坏性命令。按片段稳定 ID 绑定、记录源区间并确认范围提交。 | 可试听原声与处理结果，区间编辑与波形一致。应用、取消、撤销和换片段不会误写源文件或其他片段。 |
| 8 | 电平与响度 | 原版 `LevelMeter.set_levels(db_l, db_r, gr_db)` 接真实轨道、Bus、Master 电平及压缩衰减。UI 线程合并刷新。绑定 peak hold、削波复位、Short-term、Integrated、LRA、True Peak 和测量复位。 | 已知测试信号的 dBFS、dBTP、LUFS 在应有容差内。停播与断开后读数按约定衰减或回到未测量状态。 |
| 9 | 保存、迁移与交付 | 把生产参数保存到工程模型和版本化 schema。布局仍由 LayoutStore 保存为用户偏好。接当前导出和 Stem 流程，恢复未知字段与不可用效果提示。核对已有 Bus 自动化、Stem 取点和时间调度问题。 | 从旧工程打开、保存、重开和导出均保持编辑语义。DX、FX、MX Stem 及 Master 可重现时间位置和路由。 |
| 10 | 性能与故障验收 | 对长时间线、多个采样率、声道数、缺文件、设备切换、录音中断、项目切换和浮动窗关闭做实播与离线回归。接入外部素材前检查缓存、音画同步、延迟、资源释放及重启恢复。 | 页面频繁切换无第二条音频流，无过期回写。检测并报告欠载、导出差异和效果缺失。 |

效果器清单及原型参数见 [来源清单](../../src/features/mixer/effects/source_manifest.json) 和 [参数 schema](../../src/features/mixer/effects/parameter_schemas.json)。这些条目目前标记为 `not_connected`，预览与导出能力随真实处理器验收逐个开启。

可复用的现有入口包括 [剪辑控制器](../../src/app/controllers/edit_controller.py)、[音频命令](../../src/features/edit/core/commands/audio_commands.py)、[时间线采样时钟](../../src/features/edit/core/sample_time.py)、[音频播放](../../src/features/edit/core/audio_playback.py)、[节目监看](../../src/features/edit/ui/program_viewer.py) 和 [音频混音复审](../audit/2026-11-15-audio-mixer-review/README.md)。

首轮实测可将地下城堡对白、音效、音乐三个 44.1 kHz 双声道文件放到同一起点。对齐画面后，验证在剪辑页和混音页之间切换、前后逐帧、区间淡变、精调应用、总线监听和 Stem 导出。三份素材的概览为归一化数据，响度验收应重新测量源素材。
