# Topos 对标 DaVinci Resolve：功能加强建议（2026-10-22）

## 范围与结论

基于当前源码 HEAD `816a2d84f`、README/架构文档和 2026-10-21 的既有交互审计，并对照 Blackmagic Design 官方 Color/Edit/Fairlight/Collaboration 功能说明。本轮是源码与证据复核，没有重新启动应用做完整 GUI、导出、设备或画质测试；建议优先级是产品判断，不是实测性能排名。

Topos 已有较广的剪辑、调色、混音、字幕、合成、代理/重连、队列和 AI 基础。下一阶段最值得投入的是专业工作流深度、跨页一致性和交付可信度。功能数、节点数、测试数或后端枚举不能直接代表用户可完成的任务。

证据区分：**源码确认**＝看到了实现/限制；**建议补强**＝根据使用场景提出目标；**未确认**＝本轮未找到完整生产入口，不能据此断言全项目绝对不存在。

## 1. 发布门禁：工程、预览、导出的一致性

**现有**：统一工程包、撤销/重做、自动保存/恢复、导出 preflight、队列持久化、manifest 和输出探针。

**需要加强**：以用户任务组织端到端回归，覆盖 Edit→Color→Composite→Deliver→重启恢复。特别是嵌套、非当前序列、自定义范围、变速/音频、字幕、效果关键帧、锁轨与媒体丢失；它们涉及多套模型和宿主接线。

不把昨天审计的问题直接重列为现存 bug：当前 `src/features/deliver/ui/video_export_settings_panel.py:3056` 已有 duration_frames 的权威读取修复；`src/features/deliver/ui/deliver_workspace.py:521` 已有全量序列传递入口，runner 也传递 timeline_sequences。源码接线可确认，修复后的完整 GUI 行为本轮未重测。

**最小验收**：同一真实工程经编辑、跨页调色、保存/恢复和导出后，帧数/PTS、片段身份、效果、字幕、音频和色彩 metadata 均可核对；缺项显式阻断/报告。突然退出、磁盘写满、取消、缺媒体、旧版工程迁移有恢复策略。

## 2. 专业剪辑效率（高优先）

**源码确认**：已有基础三点、Ripple/Roll/Slip/Slide、独立 V/A 目标轨、多机位组与波形同步。`src/features/edit/ui/trim_numeric_dialog.py:157` 的 TrimMonitorDialog 主要显示两侧时间码/位移/余量；`multicam_panel.py:149` 的角度网格使用 250ms 合并刷新的缩略图。

**建设目标**：

- 切点两侧真实画面的 Trim Viewer，预/后卷循环、JKL 动态修剪、commit/cancel 与焦点正确性。
- 逐源音频流/声道→目标轨映射；视频/音频单独插入，多声道录音不要意外混入。
- 在现有三点基础上评估 backtime、目的 In/Out 定长、fit-to-fill 及四点冲突选择；通过现有命令/Undo 提交。
- 多机位先实现 4 路同钟实时监看，再评估更多路；区分固定主音轨、仅切视频和音频跟随视频。
- 波形同步当前以开头音频段和整数帧偏移为主（`src/features/edit/core/multicam_sync.py`）；增强 timecode/In/marker 备选及长录制漂移检测，不能强制对齐低置信度结果。

**验收场景**：一场多机位访谈，连续切角、修剪和固定主音轨，保存/重开与导出一致；拖动切点的两幅参考画面和最终切点相差不超过预期帧精度。

对标依据：[Resolve Edit](https://www.blackmagicdesign.com/products/davinciresolve/edit) 的专业修剪、实时多机位和独立音视频切换。

## 3. 音频精度与实时预览一致性（高优先）

**现有**：内置 DSP、Bus/Send、自动化、响度、录音、Stem/BWF 等基础。本轮不把它们列成缺失。

**源码确认的边界**：`src/features/edit/core/capability_registry.py:330` 把保调变速列为 PARTIAL，说明离线 WSOLA 已接，实时仍为变调 resample；`audio_playback.py` 的速度实现也对应这一限制。`timeline_v2.py:258/279` 的片段位置、fade 持续时间等主要以整数视频帧表达；解码端 sample-accurate seek 不等于用户具备采样级编辑。

**建设目标**：流式保调、seek/loop 状态重置、采样级音频剪辑/fade、明确声道与总线路由、自动化和 Stem 与最终混音一致。VST3/AU 宿主可后排，需连带延迟补偿、插件状态恢复与崩溃隔离。

**验收场景**：48kHz 对白精细修口型、0.5/1.25/2 倍语音预听及导出，切换/loop 无爆音；Stem 按声明的路由重新合成后可解释地重建成片混音。

对标依据：[Resolve Fairlight](https://www.blackmagicdesign.com/products/davinciresolve/fairlight)。具体 Topos 目标以上述源码边界为依据，不要求照搬全部大型录音棚能力。

## 4. 多镜头调色工作流（高优先，Color 产品可优先于新增剪辑工具）

**现有**：一级/二级、曲线、LUT、OCIO/ACEScg、示波器、跟踪、降噪、Gallery/reference wipe；`src/features/edit/core/commands.py:16677` 已有 UseSharedGradeCommand，不能声称“没有共享调色”。

**建议补强**：场景/机位组管理、组前/片段/组后或等价的清晰处理阶段、共享 look 的锁定/解除、镜头命名版本与一键回退、多镜头 lightbox 比较、批量匹配后的逐镜头修正。当前检索主要确认了素材/片段 stack 与共享 grade，没有确认上述整套工作流的成熟生产入口；实施前先做专项调用链/产品交互盘点。

**源码确认的局部限制**：`src/features/color/core/preset_meta.py:8` 的持久化 Gallery still 为最长边 1920 的 JPEG，`load_still_image()` 由真实参考图入口消费。JPEG 可用于浏览，但 HDR 精确比较需要另外保存可保留线性高动态范围及色彩语义的参考数据，不能把 sidecar 的色彩标签当成图像精度恢复。

**验收场景**：同一场景多个不同机位镜头共同改 look，保留各镜头曝光微调；共享/独立版本、撤销、保存重开和批量导出一致。HDR 参考在同一显示变换下比较。

对标依据：[Resolve Color](https://www.blackmagicdesign.com/products/davinciresolve/color) 的 grouped clips、shared nodes、gallery、versions 和 lightbox。Topos 可以用适合自身模型的实现，不必先改成相同 UI。

## 5. 色彩管理、监看与相机 RAW（高优先，按目标用户取舍）

**现有**：OCIO/ACEScg、PQ/HLG/P3 及显式色彩标签、RAW 面板、相机/媒体格式分类、DeckLink CPU 输出。

**建议补强**：让输入解释→工作空间→显示变换→输出变换→编码标签形成可验收链；真实测试 SDR/HDR、full/limited、代理/原片切换和外接监看。区分 UI 屏幕和校准监看路径，避免重复 tone mapping、range 或 gamma 处理。广播/院线认证和高级交付标准按实际用户需求单独立项。

`src/shared/media/format_registry.py:5` 明确说明扩展名分类不保证后端能打开，`ffmpeg_source.py:1131` 起对部分 BRAW/R3D 缺解码支持有诊断。相机 RAW 支持要按格式/版本/SDK/平台列出真实打开、源级白平衡/曝光、代理和导出覆盖；照片 RAW/Topos RAW 不替代所有电影相机 RAW。

DeckLink GPU 直送目前仍是占位能力，详见已有性能审查；专业监看质量与是否零拷贝要分别验证，不因 CPU 输出就自动判为不可用。

**验收场景**：一组含肤色、高亮、超范围和渐变的相机素材，经两种平台的预览/监看/导出仍保持声明的变换与标签；为已承诺的相机原片建立真文件回归。

## 6. 跨软件交接与 reconform（高优先，专业团队采用的关键）

**源码确认**：FCPXML/OTIO 有适配器和保留/转换/丢弃报告；`src/features/edit/core/capability_registry.py:296` 的 AAF 明确为 UNAVAILABLE。

**建设目标**：先完善 FCPXML/OTIO 的实际常用子集及往返，再按声音后期需求建设 AAF。重点是源时码/卷名、混合帧率、handles、声道、markers、速度映射、嵌套和回原片重套；无法表达的效果/变速必须提示 bake/flatten/drop，不能静默损失。

**验收场景**：外部 NLE→Topos 调色/精修→回原软件，逐切点、源帧身份、时码和音频对齐可复核；AAF 用真实接收软件打开验收。优先打通一个受支持工作流，避免同时承诺所有交换格式。

## 7. 合成、跟踪与时域效果（以镜头质量验证深化）

**现有**：Composite 节点、跟踪/抠像/绘制、3D/AOV、缓存以及多种 AI 节点。README 的节点数为项目声明，本轮未逐个做真实 GPU/导出验收。

**建议补强**：用困难真实镜头衡量跟踪漂移、遮挡后恢复、修补/抠像边缘、去噪细节/鬼影、补帧遮挡和时间稳定性；明确手动修正、缓存失效和 render-in-place 的行为。复杂图的交互、缓存与生产导出是否一致应先于继续扩节点清单。

**验收场景**：带遮挡、运动模糊、细发丝和透明边缘的镜头，从 Composite 回 Edit 并导出；随机 seek 与顺序播放的结果符合各效果的时域定义。

## 8. 工程版本与审片协作（中优先，分阶段建设）

**现有**：本地工程持久化、自动保存/备份及恢复。它们不应被写成缺失，也不等于成熟多人协作。

**建议路线**：命名快照/版本比较→带帧号评论/审片导入为 marker→共享工程只读/写入锁→有需求后再做并发冲突与合并。当前未确认完整多人会话、权限和冲突解决生产链；教学动画或 AI 模块里的 collaborative 字样/契约不能直接算全项目协作已完成。

**验收场景**：客户评论准确落到指定版本帧；两人不能静默覆盖同一工作；版本切换保留素材与效果依赖，恢复后评论锚点可解释。

对标依据：[Resolve Collaboration](https://www.blackmagicdesign.com/products/davinciresolve/collaboration) 的锁定、timeline comparison、共享 markers 和协作工作流。

## 9. AI 与交付自动化的差异化（核心稳定后推进）

转录、字幕、AI 计划和工作流契约已有基础；应优先完成能稳定提交到现有模型的用户任务，例如文稿驱动粗剪、删口头语、字幕多语交付、带置信度的镜头匹配和可恢复批处理。先确认实际 UI→命令→渲染→保存/导出接线，不把 typed plan/registry 当完整功能。

验收包括可预览、可取消、可撤销、低置信度人工修正、重复执行幂等及资产/模型不可用的清晰诊断。专业交付还可继续深化多版本/声轨/字幕组合、响度与黑帧/静帧/缺帧 QC、报告与可重试任务；现有 preflight/manifest/QC 是复用起点。

## 推荐推进顺序

1. **发布门禁先行**：给真实工程保存/恢复、跨页和交付建立黄金项目；性能工作沿 A/B v3，功能验收与性能验收分开。
2. **优先三个功能包**：专业修剪/多机位；实时音频保调与精度；多镜头调色/版本管理。若先做 Color 专注版，先推进调色与色彩监看包。
3. **专业项目准入**：交换往返/reconform、目标相机 RAW、监看/交付矩阵；按真实用户项目选范围。
4. **后续扩展**：命名版本与审片、AI 任务闭环；多人实时协作、完整第三方音频插件宿主和大型 3D 扩展根据需求立项。

每包先定义一个可完成的用户任务、正确性标准、失败恢复与真实工程验收，再估实施范围。保持已有模型/command/service 和资源线程边界；不要以新增面板或类型声明代替生产能力。
