# 第七轮交付/G14/Q01/专业能力复核

日期 2026-10-22，基线 d11615bb8，只审计；没有改业务代码/既有测试。

## 已执行与通过

`.venv/bin/python -m pytest tests/edit/test_fix_round6_g14_image_sequence.py tests/edit/test_fix_round6_q01_decoder_lifecycle.py tests/deliver/test_delivery_capability.py -q`：**26 passed in 5.29s**，日志 `/tmp/topos-round7-delivery-tests.log`。这不是全量单进程验证。

新增独立 probe `/tmp/topos-round7-delivery-probe.py`（结果 .json 同名），真实音视频 2 帧端到端 `/tmp/topos-round7-delivery-e2e.py`（.json/.log 同名）。实际编码器无替身；端到端采用真实 TimelineSequence/MediaAsset/TimelineExporter、真实 tests/test_videos/2K_h264.mp4，FrameProvider 接口传 Mock，导出器真实文件后备路径解码，非真实鼠标入口。

- PNG8、TIFF8、TIFF16 编码器直接回读 RGBA 像素逐值等于原输入（含alpha）。
- 真实 TimelineExporter PNG/TIFF 各成功导出2帧；基础序列能力已实现，不应再称完全缺失。
- D10 enum/string 提取已修，逐帧产物 no-audio 声明已修。
- Q01 三个 focused 生命周期用例本次均通过；根因仍待全套顺序复现。

## 确证问题（建议加入新计划）

### IMG01 P1：同名序列覆盖原帧，取消进一步删除原产物

位置 `src/shared/export/image_sequence_encoder.py:150`，`:84–94`。`encode_frame`直接`open(path,'wb')`覆盖已有帧，`abort`对_written无条件remove，没有备份/归属/冲突策略。临时夹具预写shot_000000.png='EXISTING USER FRAME'，导出一帧后内容改变，abort后该已有文件彻底不在。只操作新建临时目录，未损坏用户媒体。多次不同长度导出同基名还会留下旧尾帧，外部软件会读到混合序列。生产队列的目标路径检查不能覆盖每个实际帧，目录基路径通常根本不存在。

建议：导出前解析全部预期帧目标并冲突预检；独立暂存目录成功后原子提交；取消只清本次新建文件，覆盖需明确授权策略并可恢复。验收：旧序列逐文件hash在取消/失败后不变，长→短无旧尾帧，ASK/SKIP/OVERWRITE各自真实生效。

### IMG02 P2：合法目录目标写到父目录，manifest也无法核验图序

位置 `image_sequence_encoder.py:48–54`把output_path一概拆dirname/basename；`timeline_export.py:574–585`和`video_export_preflight.py:505–507`明确接受现有目录。真实端到端给已建目录`.../png_sequence`，成功返回但该目录为空，产物却在父目录`png_sequence_000000.png`。TIFF同样。日志`Manifest output hash failed: [Errno21] Is a directory`。manifest沿用视频单文件探测（`export_manifest.py:207–235`），没有列出/哈希真实帧文件；即使传基路径也不存在，present=false。

建议：目录/图像基名语义统一；manifest实现序列专用schema，保存帧清单、fps、范围、首尾解码及hash。验收：已建目录/新目录/带扩展名基路径各自文档一致，UI显示目标位置就是实际位置，manifest完整核验所有输出帧。

### IMG03 P2：宣称16-bit/可配帧号，真实配置桥未贯通

位置 `timeline_export.py:4943`读取`getattr(config,'bit_depth',8)`，但TimelineExportConfig dataclass没有bit_depth字段；`video_export_job.py:793–838`也不透传video_config.bit_depth，UI legacy位深控件隐藏且无16bit选项。正式TIFF配置始终rgb24，故16-bit只在直接给编码器uint16的单元测试通过，正常导出入口不可配置。

同类配置断点：`image_sequence_encoder.py:56–60`读取naming_config/frame_number_start，但实际交给encoder的是FFmpegEncoderConfig，无naming_config/start_frame。真实TimelineExportConfig(start_frame=23,unique_filename_digits=9)经_create_encoder_config→_create_encoder后编码器_start_index=0/_digits=6。注意unique_filename_digits原语义是individual文件去重，并非图序专用，不能直接冒充原已有图序帧号UI。应明确增加序列命名/起始号并桥接，而非仅改getattr。

验收：真实queue保存重开后可选择8/16位，输出文件format与像素精度相符；可配置图序start index/padding并落盘一致。

### IMG04 P2：PNG16字节序转换损坏像素（编码器级，当前普通UI不可达）

位置 `image_sequence_encoder.py:131–142`。byteswap保持native uint16数值后又交PyAV.from_ndarray(format='rgba64be')，PyAV本身处理端序，造成重复交换。真实RGBA16输入像素[30,250,100,200]，编码PNG再PyAV解码为rgba64le得到[7680,64000,25600,51200]；TIFF16同输入完全正确。普通生产入口尚被IMG03限制8bit，因此不能夸大成当前GUI所有PNG导出都会错。

建议：按PyAV API的数值/端序契约传数组；真实16bit ramp及随机值回读逐值比较，RGB和RGBA都测，alpha同样不能损坏。

### IMG05 P2：图序编码器未声明supports_audio=False，音频省略结果谎报

位置 `image_sequence_encoder.py:class`缺supports_audio；`timeline_export.py:3004`默认True，进入不存在的add_audio_stream（3030 catch后has_audio=False）。真实带V/A TimelineExporter导出PNG和TIFF都日志`Failed to add audio stream: ... no attribute add_audio_stream`，结果success=True且**audio_omitted=False**；实际当然无音轨。preflight已认识图序限制，不等于worker结果正确。

建议：显式声明无音轨，复用已有omitted分支，队列结果/manifest明确无音频或配套sidecar策略。验收：带A音轨include_audio=True的图序输出不触发异常，audio_omitted=True且用户可见降级；include_audio=False不误报。

### CAP01 P3：D10仍有容器/alpha残余误报，当前未接生产消费

`delivery_capability.py:81–86`仅mp4/mov二选一，GIF/PNG/TIFF都报告container='mov'；ALPHA_CAPABLE_CODECS不含PNG/TIFF，has_alpha=True仍alpha_capable=False，实际编码器RGBA8往返已通过。`rg describe_delivery_capability src`仅定义，当前尚无生产调用方，不能写成UI阻断bug。建议单一codec能力表同时描述输出形态、容器/无容器、alpha及位深，未知明确unknown。验收真实config与实际输出/codec registry契约一致，枚举/字符串不仅彼此相等而且事实正确。

### QA01 P2：Q01只是栈诊断，尚非能阻断挂起的超时门禁

`pytest.ini:16`faulthandler_timeout=300只dump线程栈，不终止挂住用例（[pytest官方说明](https://docs.pytest.org/en/stable/how-to/failures.html#fault-handler)）。新增test_close...对close耗时断言位于close返回后，若container永久等待永远到不了assert。`test_fix_round6_q01_decoder_lifecycle.py:105–110`更在`del source`之后才启动计时，引用计数触发的__del__耗时可能已被漏计。`:84 closed.append(source)`持有全部源，使循环gc.collect并没有析构这些对象（不过显式close本身确实在测）。不能说压力测试无价值，但不能据此关闭原根因或宣称已有有界门禁。

建议：独立子进程超时+终止/栈捕获门禁，正确覆盖del前计时和worker仍活跃的竞争场景，保留有序全套复现。验收：故意注入阻塞的close也能在预算内失败退出；干净单进程全套自然结束；根因关闭另有证据。

## 下一阶段专业能力更新（当前源码+官方工作流基准）

下面不是新增bug清单；顺序是工程建议，不声称所有专业软件都必须同时具有所有功能。除G14/G16相关文件，本轮提交未实现G01-G13/G15/G17；源码抽查与执行进度一致。

| 原ID | 当前状态 | 后续可验收工作 |
|---|---|---|
| G01 | 部分：已有V/A目标轨，无逐源流/声道patch | 源声道1/3映射A1/A3、video-only/audio-only Insert/Overwrite，一次undo；[Adobe Source Patching](https://helpx.adobe.com/in/premiere/desktop/edit-projects/intro-to-editing/add-media-to-the-timeline-using-source-patching.html) |
| G02 | 部分：基本三点已存在，backtime/fit-to-fill未新增 | 源In+目标In/Out定长、目的Out倒推、四点冲突选择；[Apple三点编辑](https://support.apple.com/en-ca/guide/final-cut-pro/ver549f253e/mac) |
| G03 | 部分：TrimMonitor仍只有QLabel时间码，无真实A/B画面 | 真实切点前后帧、JKL动态trim、预后卷循环；`trim_numeric_dialog.py:157–224`；[Resolve Edit](https://www.blackmagicdesign.com/products/davinciresolve/edit/) |
| G04 | 部分：多机位切角已有，网格250ms缩略图合并 | 4/9路共同时钟实时画面，视频/音频独立切角/AFV；`multicam_panel.py:149,317`；[Resolve Edit](https://www.blackmagicdesign.com/products/davinciresolve/edit/) |
| G05 | 部分：波形前60秒、±5秒固定offset | TC/In/marker同步、长录制起末漂移检测；`multicam_sync.py:132,254–255`，失败不给伪offset |
| G06 | 部分：离线保调存在，实时仍resample且UI export only | 0.5/1.25/2x预览与离线音高时长一致；`audio_playback.py:1403`、properties4039；[Apple保调](https://support.apple.com/en-ae/guide/final-cut-pro/verb6aca45d/mac) |
| G07 | 部分：decoder可样本精确，剪辑位置仍整数帧 | 48kHz一sample移动/trim/fade，undo保存stem精确保留 |
| G08 | 缺AAF交换 | 轨位、源TC、声道、handles、自动化，真实ProTools导入；capability_registry.py297；[Adobe AAF](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-aaf-files.html) |
| G09 | 部分：FCPXML/OTIO受限支持，不是完全缺失 | 外部黄金工程往返，nested/retime/multicam/音频路由丢失明示、reconform；fcpxml_adapter.py19 |
| G10 | 有ASR字幕/AI基础，无文稿工作区直接剪媒体闭环 | 逐词删除走linkedAV字幕marker事务；[Premiere文稿剪辑](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html) |
| G11 | 无通用视频候选take容器；ADR试听不等价 | 多候选同位置可比较与保存，交换flatten；[Apple Auditions](https://support.apple.com/guide/final-cut-pro/intro-to-auditions-verbbd3587d/mac) |
| G12 | 有自动保存备份，无完整多人冲突/审片回流 | 先命名快照、评论→marker，再项目锁合并；[Resolve协作](https://www.blackmagicdesign.com/products/davinciresolve/collaboration/) |
| G13 | 部分：批量日志字段已有，混合素材Properties仍不支持 | 混合值提示、批量解释帧率/声道、一次事务；edit_properties_panel.py2104 / edit_controller.py5765 |
| G14 | 从缺失变部分可用，不能按完成关单 | PNG/TIFF8基础真实导出已通过，优先关闭上述IMG01–05；再外部合成软件回导、命名、帧范围、颜色元数据 |
| G15 | 内置DSP存在，未新增VST3/AU宿主 | 插件扫描/隔离/延迟补偿/状态恢复/离线一致，后期建设 |
| G16 | 新增Transform/Opacity◆入口（核心agent另核） | 不再列为完全无动画UI；仍需按完整验收核曲线/关键帧导航/preview-export/保存undo一致 |
| G17 | 部分：嵌套基础可用，音频展开仍清子转场 | nested_sequence.py842 transitions=()；parentretime/mixedFPS重采样/父子导航完整性仍需专项。音频agent详核，避免重复计数 |

优先顺序：现有输出正确性和安全→G01/G02/G03/G06/G17→G04/G05/G07/G08/G09/G13→G10/G11/G12/G15。外部软件真实导入、长时4K/多路播放和真实声卡耳听本子任务未做，不用源码存在代替实际验收。
