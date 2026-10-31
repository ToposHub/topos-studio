# 2026-10-21 剪辑音频、字幕、交付专项审计

只读产品审计，未修改产品/测试代码；已阅读 `.clinerules`、第五轮计划及修复记录。此专项未运行 GUI，所有下述实测均为生产领域函数/渲染器调用，若为替身 decoder 会明确标注。最终 GUI 结论由主代理补充。工作目录为当前项目，Python 为项目 `.venv/bin/python`。

## 已支持能力（不要当作“缺失”重报）

- 8 个内置音频效果（EQ、compressor、limiter、reverb、delay、saturation、imager、noise gate），可挂片段与轨道；轨/Bus/Master 混音、solo 闭包、音量/声像/静音 automation 已有。
- Audio Gain 与 LUFS/true-peak 分析归一化已有用户入口（但发现本轮新缺陷）。离线 WSOLA preserve-pitch 已有；实时 preserve-pitch 仅披露为 export only，这是仍存在的能力边界。
- 音频分轨 Stem 导出（Master/Track/Bus/D/M/E）已有 File 菜单/对话框/worker，不是缺失；但与成片不一致见 D05。
- 字幕有多语言/角色/QC/样式/翻译/词级时间码/ASS karaoke，支持 SRT/VTT/ASS/TTML/DFXP/SCC 导出、字幕烧录，MOV/MP4/MKV 文本字幕 mux 命令构造。支持 SRT/VTT/ASS/SSA/TTML/DFXP/XML 导入。
- FCPXML/OTIO 有受限子集导入/导出及映射报告（OTIO 依赖可选）；AAF/OMF 注册为 unavailable。
- 多机位/嵌套已实现；多机位是静帧网格，缺实时多路、时间码/标记同步、音频跟随切换；嵌套 retime 音频有明确限制。
- 自研 Topos、H.264/HEVC、ProRes/DNxHR 导出、批量队列、导出 manifest/交付 preflight/QC 已有，不应笼统写“缺专业导出”。

## 新确认问题清单

### D01 / P1 音频 retime 在导出中没有与片段视频映射共享规则

位置：`src/features/edit/core/timeline_export.py:4257`，读源映射 `:4261-4266`；WSOLA 起点也只看 scalar `:3356`。Stem 生产 decode 同问题在 `src/features/edit/core/stem_export.py:841-849`。实时只注入 scalar speed（`edit_page.py:1276-1290`），无法表示 reverse/speed_points，需统一专项回归。

实测1：真实 `tests/test_videos/2K_h264.mp4`，源起点100帧，25fps，50帧音频。生产 `_render_audio_master` 分别渲染普通、reverse=True、speed_points=(source_offset25→2x) 三种时间线，PCM shape 都为 `[2,96000]`，peak均0.10838188976049423，三份 PCM 逐样本完全一致，maxdiff=0。规范 `ClipInstance.timeline_to_source_frame`：普通100→149，reverse149→100，ramp末帧173。

实测2：补充48kHz/440Hz正弦 WAV（/tmp合成，非用户素材）。preserve_pitch=False 时1x FFT主峰440Hz；2x应880Hz却430Hz；0.5x应220Hz却445Hz。导出只是跳帧/重读片段拼接1x块，没有进行样本域变调重采样。

影响：带声素材倒放、速度曲线、普通快慢动作成片音画语义错误；preview/export也不一致。不是第五轮 E15“实时保调未实现”同一个问题。

建议：音视频/Stem 统一使用 canonical 连续时间映射；样本级 resample/reverse/曲线，preserve-pitch 独立 stretch；禁用的组合应在 preflight 明确拒绝。验收：带时间/频率标记的音视频分别验证0.5x/2x/reverse/ramp/smooth ramp、非零入点、拆分和 trim，输出音频与视频读源一致；2x未保调440→880Hz、保调≈440Hz。

证据：`/tmp/topos_delivery_real_audio_20261021.py`、`/tmp/topos_delivery_probe_20261021.py`、`/tmp/topos_audio_speed_sine_20261021.py`。

### D02 / P1 Audio Gain 归一化先混单声道，错误测量立体声，反相素材会被大幅增益

位置：`src/features/edit/ui/timeline_view.py:11663` 的 ffmpeg `-ac 1`；`:11674` reshape(1,-1)；静音floor参与建议 `:11694`，UI夹值 `:11597`。

实测生产 `_analyze_clip_normalize_gain`：4s立体声440Hz，L=0.3sin，分别R=L与R=-L。原立体声两种布局实际BS.1770均-14.1510577LUFS，应增益-8.8489423dB（离-3dBTP峰限尚远）。同相被UI测为-11.1LUFS、建议-11.858385dB；反相被混成静音，UI显示-70LUFS、-inf dBTP，建议+47dB，实际控件夹到+12dB。这会把实际有声的反相立体声大幅放大，峰值约束完全失去意义。

建议：保留真实声道布局，按独立声道能量进行BS.1770与true-peak分析；静音/低于测量门限拒绝建议正增益，不能把floor当实际响度。验收：同相/反相/不相关stereo、mono、5.1，分析与参考表一致，归一化后的实际音频到目标且各声道true peak不超上限。

证据：`/tmp/topos_normalize_probe_20261021.py`。

### D03 / P2 Audio Gain 的分析同步阻塞 UI，取消按钮在解码和DSP中无事件机会

位置：`timeline_view.py:11580-11588` 从 clicked 槽直接调用；`11667-11668` subprocess.run(capture_output=True,timeout=120)；`11681-11687` 同步分析只读取wasCanceled，没有事件泵/worker。

证据等级：S（完整调用链确认）；建议主代理用真实窗口QTimer heartbeat和真实大素材补G级。点击“分析并归一化”后只有调用前一次 QApplication.processEvents；120s阻塞解码期间不能响应取消，后续DSP同线程也无事件泵。还会一次性保留整段 PCM，长素材内存随时长增长。

建议：搬入可取消worker，流式解码/分块分析，取消终止子进程、丢弃结果、释放资源；UI只消费进度。验收：长素材分析时拖窗/点击取消响应<200ms，取消无历史提交，短时/失败均清理。

### D04 / P2 音频片段效果关键帧保存有效但声音完全忽略

位置：属性面板把音频旧ParamSchema转换为可动画ParamDef `edit_properties_panel.py:4089-4112`；数值◆创建 `:4709`。实时同步 `edit_page.py:1367-1375` 只静态params；导出 `timeline_export.py:4191-4207` 仅set_params一次，未求值animated_params。

实测：生产effect模型/音频master，decoder为恒定0.5替身（明确非真实素材）。Limiter静态threshold=0输出0.5；在同base上添加frame0 threshold=-20关键帧，模型get_param_at_frame=-20，但输出仍0.5，PCM逐样本等于无关键帧；直接静态threshold=-20输出0.1。UI对这些参数提供◆但音频完全不消费。

建议：在统一AudioGraph路径中求值片段效果动画，分块平滑参数并保DSP连续状态；若暂不支持则禁用音频FX◆并清楚披露，不允许保存后悄悄忽略。验收：limiter/compressor/EQ keyframe实际改变声音，preview/export/Stem一致，undo/redo和保存重开保持。

证据：`/tmp/topos_audio_fx_keys_20261021.py`。

### D05 / P1 Stem 导出忽略片段效果与冻结静音，交付物与成片不同

位置：`stem_export.py:333-348` 收集所有clip未检查freeze；`:417-420` 调render_track_block未传clip_fx_chain，也未附音频crossfade；生产decode `:810-850` 不检查freeze。

实测真实2K_h264源100起50帧，生产create_clip_decode_fn+render_stem_pcm，对比生产timeline master：

| 场景 | 成片master peak | Master stem peak | 最大逐样本差 |
|---|---:|---:|---:|
| limiter片段FX | 0.1000000015 | 0.1083818898 | 0.0237935707 |
| freeze_source_frame=100 | 0 | 0.1083818898 | 0.1083818898 |

补充DC decoder能放大定位：limiter成片0.1 vs stem0.5；冻结成片0 vs stem0.5。Stem注释声称per-clip FX已由render_stem_pcm处理，实际没有参数传入。音频转场也缺少包络构建，但后者本专项未动态单测。

建议：统一音频图/clip graph渲染，不维护Stem独立简化逻辑；把clip-FX、freeze、crossfade、变速/保调全部纳入。验收：相同范围Master stem与成片解码PCM匹配，Track/Bus/DME stem等价solo，覆盖上列所有片段语义。

证据：`/tmp/topos_stem_real_parity_20261021.py`、`/tmp/topos_stem_parity_20261021.py`。

### D06 / P2 非整数帧率音频导出每帧之间插入单采样静音洞

位置：`timeline_export.py:4080` 固定 floor(sr/fps)，`:4273-4277` 用精确frame→sample起点，但块长度始终floor。Stem `:282`、`:378`同结构。

实测生产master+恒定非零decoder，300帧、48kHz：25fps输出576000samples、静音洞0；30000/1001输出480480samples、每声道180个额外零样本，起点[3202,6405,8007,11210,...]；60000/1001输出240240samples、额外零样本240个。此为离散点击/波形连续性风险，未做耳听断言，不能声称已听到噪音。

建议：每帧所需样本数=精确boundary(f+1)-boundary(f)，解码和落位同源；stretch cursor也使用该长度。验收：NTSC 23.976/29.97/59.94长短项目，恒定信号无额外零洞、正弦连续，samples总长精确，preview/export/Stem一致。

证据：`/tmp/topos_delivery_samples_probe_20261021.py`。

### D07 / P2 取消音频渲染跳过 decoder.close

位置：`timeline_export.py:4237-4238` return None 提前退出；正常cleanup在`:4375-4380`，未用finally。

实测：记录decoder在第一次decode时将_cancel_requested=True，第二次循环函数返回None，opened decoder.closed=False。说明应用没有明确执行资源释放；不能夸大成“已证明每次永久泄漏”，PyAV可能最终由GC关闭，但违反取消即时清理生命周期。

建议：decoder管理置于try/finally或ExitStack，成功/异常/取消统一关闭；WSOLA预解码循环也检查取消。验收：取消各阶段/解码失败时所有opened decoder.close一次，文件句柄/线程归零，未写成功manifest。

证据：`/tmp/topos_delivery_samples_probe_20261021.py`。

### D08 / P1 ASS 导入遇英文逗号静默丢字幕前半句，项目自己导出后重导也损失

位置：`src/features/edit/core/captions/importer.py:132` split(',',10)，`:144`只取fields[-1]。

实测：生产render_subtitle_track把“Hello, world”导出标准ASS，生产parse_subtitle_text重导仅剩“world”；“你好，世界, Good morning”只剩“Good morning”。ASS事件固定前9个分隔，文本部分本身允许逗号。当前多split一次，将Text第一段当另一个字段丢弃。

建议：读Events Format列描述，按字段数-1切分（标准ASS为maxsplit=9），完整保留Text，SSA也按声明格式解析。验收：含1/多个ASCII逗号、中英文、换行/ASS标签的导入与自己导出往返，文本逐字不丢。

证据：`/tmp/topos_caption_probe_20261021.py`、`/tmp/topos_caption_evidence_20261021.jsonl`。

### D09 / P2 TTML 导入忽略文件timebase，tick与frame单位时轴错位

位置：`captions/importer.py:186-189`将f按目标时间线fps转换，将t直接当ms；`:212-245`根节点从未读取ttp:frameRate/frameRateMultiplier/tickRate。

实测：目标24fps；TTML ttp:tickRate=10，10t→20t本应1→2秒=24起/24帧，实际0起/1帧；ttp:frameRate=30，30f→60f本应24起/24帧，实际30起/30帧。TTML文件声明被静默忽略。

建议：解析文件时间基准后换算到目标序列fps；支持的clock/offset-frame/tick定义需一致，无法支持的timeBase明确拒绝，不能静默用目标fps代替。验收：W3C参考向量、30→24、30000/1001、tickRate、嵌套begin、dur/end、frameRateMultiplier均正确。

证据同D08。

### D10 / P3 统一DeliveryCapability对真实ExportCodec枚举给出错误报告，且尚未生产接线

位置：`src/features/deliver/core/delivery_capability.py:72-75`先str(codec)再取value，导致枚举信息丢失。rg显示当前只有定义和测试，无生产消费者，故不当作现有UI可达故障。

实测真实TimelineExportConfig：GIF codec="exportcodec.gif"、container="mov"、audio_capable=True；TOPOS_IMAGE_SEQUENCE同样声称可含音轨；PRORES_4444+has_alpha=True却alpha_capable=False；H264 container=mov。现有测试都用SimpleNamespace codec字符串，绕开生产类型。

建议：先取原始codec.value再str；使用生产TimelineExportConfig参数化测试；生产入口接线前纳入契约门禁。验收：codec名、容器、audio/alpha/HDR与实际encoder一致；enum/string结果相同。

## 本轮测试结果

命令：

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/deliver tests/edit/test_audio_export_parity.py tests/edit/test_audio_timestretch.py tests/edit/test_audio_automation_recording.py tests/edit/test_audio_clip_effects.py tests/edit/test_clip_audio_fade.py tests/edit/test_clip_audio_gain.py tests/edit/test_caption_export.py tests/edit/test_caption_quality.py tests/edit/test_caption_global_style.py tests/edit/test_smooth_speed_ramp.py tests/edit/test_timeline_clip_speed.py tests/edit/test_export_video_effects_preflight.py tests/edit/test_effect_render_operations.py
```

结果 **330 passed / 1 failed / 14.54s**。日志`/tmp/topos_delivery_pytest_20261021.log`。未将其误报为“全量剪辑测试通过”。

失败：`tests/deliver/test_video_export_preflight.py::TestToposVideoOnlyPreflight::test_nested_audio_triggers_topos_video_only_preflight`。`:405-407` child mock是SimpleNamespace且缺fps，`nested_sequence.py:799`访问child.fps产生AttributeError。此为测试夹具与生产模型契约不匹配，需更新夹具为完整TimelineSequence，不能据此认定真实工程嵌套导出失败。

所有专项probe均只运行生产领域函数/真实音频解码或记录替身，不触碰GPU、UI自动化与产品修改。合并输出 `/tmp/topos_delivery_evidence_20261021.json`；字幕输出另为jsonl。暂时不包含完整成片编码/用户可听输出结论。

## 专业功能差距：本专项可据代码确认的边界

| 能力 | 项目现状 | 建议优先级/验收方向 |
|---|---|---|
| 可靠统一音频交付 | mixer/core已丰富，但retime、FX动画、Stem语义分裂 | 最高：先修D01-D07，重用一个音频图/样本时基 |
| 实时保持音调 | 离线WSOLA，实时export-only披露 | P2：流式time-stretch+seek/reset，实时与导出音调一致 |
| 多机位实时多画面/TC同步/Audio follows video | 角度切换/波形同步/静帧网格已实现，三项未实现 | P2：多路监看/时间码与标记对齐/选角切声音，需硬件性能预算 |
| AAF/OMF后期交换 | unavailable；FCPXML/OTIO有限子集已有 | P2：优先AAF音频交接，真实Pro Tools/Resolve导入验收，OMF可后排 |
| Surround/沉浸音频空间声像 | 1/2/6/8声道布局/矩阵/Stem已有，>2声道pan仅trim，完整surround panner缺失 | P2/P3：5.1/7.1轨道声像、布局meter与导出一致；Atmos/ADM需单独评估，不应误称已有 |
| 第三方专业音频插件 | 有内部8个DSP，未查到VST3/Audio Unit宿主 | P3：先声明可用格式/扫描隔离/插件状态恢复/离线渲染一致 |
| Auto-ducking | 有sidechain_ducker.py纯DSP与测试，生产全仓0消费 | P2：从“有算法”变为可达工作流；旁白压音乐可试听、可撤销、可导出 |
| 专业字幕兼容性 | 常见格式及QC已有，ASS/TTML时基存在数据损失/错误 | P1/P2：先D08-D09；SCC广播完整语义/真实播出链另验，不能仅“有字符串”就对标合规 |
| PNG/TIFF序列 | 导出明确拒绝且UI披露；Topos image sequence已有 | P3：标准序列写入、alpha/HDR/帧号范围和失败恢复 |
| 批量素材属性/音频代理 | 已披露未支持/禁用 | 按真实工作流需求补，不能将披露修复等同功能实现 |

未对真实声卡、监听设备、ASIO/CoreAudio延迟、5.1声道接线耳听、广播闭合字幕合规、云协作做验收。

## 复制清单（所有路径均为/tmp，可由主代理复制进项目审计目录）

- 报告：`/tmp/topos_edit_delivery_audit_20261021.md`
- 聚合结果：`/tmp/topos_delivery_evidence_20261021.json`（8个音频probe均exit0、逐条stdout/stderr）
- 字幕结果：`/tmp/topos_caption_evidence_20261021.jsonl`
- 能力契约结果：`/tmp/topos_capability_evidence_20261021.jsonl`
- 回归日志：`/tmp/topos_delivery_pytest_20261021.log`
- D01 mock映射：`/tmp/topos_delivery_probe_20261021.py`
- D01 真实音频：`/tmp/topos_delivery_real_audio_20261021.py`
- D01 恒速合成正弦：`/tmp/topos_audio_speed_sine_20261021.py`（自动生成`/tmp/topos_audio440_audit.wav`，6秒mono440Hz）
- D02 响度合成声道：`/tmp/topos_normalize_probe_20261021.py`（自动生成`/tmp/topos_inphase_audit.wav`、`/tmp/topos_antiphase_audit.wav`，4秒stereo440Hz）
- D04 mock FX关键帧：`/tmp/topos_audio_fx_keys_20261021.py`
- D05 真实音频Stem：`/tmp/topos_stem_real_parity_20261021.py`
- D05 mock DC放大定位：`/tmp/topos_stem_parity_20261021.py`
- D06/D07 mock采样/取消：`/tmp/topos_delivery_samples_probe_20261021.py`
- D08/D09 模型字幕往返：`/tmp/topos_caption_probe_20261021.py`
- D10 真实配置类型契约：`/tmp/topos_capability_probe_20261021.py`

脚本假设工作目录为项目根，从其他目录执行需先切换项目根；原始视频路径按当前cwd拼接。均不修改产品或用户素材，合成WAV只写/tmp。D03仅S级，需要主代理GUI覆盖。D01/D05“真实音频”指真实解码PCM，没有做完整视频编码。
