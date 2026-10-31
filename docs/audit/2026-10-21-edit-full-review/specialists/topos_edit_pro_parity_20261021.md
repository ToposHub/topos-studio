# Topos Edit 专业剪辑功能对标专项（2026-10-21）

范围：只读源码核查 + 官方产品文档核对。**本专项未执行 GUI，因此以下“缺口/部分支持”不计作交互实测故障。** 事实基于当前工作树；第五轮 E01–E22 修复记录已阅读，历史已披露的产品边界不重新包装成新 bug。优先级代表建议的产品建设顺序，并非所有项目都适合马上实现。

## 已有能力（不应误报为缺失）

| 能力 | 当前证据 |
|---|---|
| 基本三点剪辑、Insert/Overwrite/Append、独立视频/音频目标轨 | `src/features/edit/ui/edit_page.py:2666`、`:2700`、`:2734`；`src/features/edit/ui/timeline_view.py:16510`、`:16530`、`:16653`。当前组合是源 In+Out 与时间线播放头；可自动插入带链接 A/V。 |
| Ripple/Roll/Slip/Slide、步进/数值 trim、Sync Lock | `src/features/edit/ui/timeline_view.py:4412`、`:9348`、`:9667`，以及 `src/features/edit/ui/trim_numeric_dialog.py`。不能因高级 trim 不完整就称“没有专业修剪”。 |
| 多机位组、1–9 切角、可撤销切点、混帧率映射、音频波形同步 | `src/features/edit/core/multicam.py:187`；`src/app/controllers/edit_controller.py:7325`；`src/features/edit/core/commands.py:2945`；`src/features/edit/core/multicam_sync.py:127`。 |
| 嵌套序列 | `src/features/edit/core/nested_sequence.py:1` 明确生产接线：创建事务、预览 resolver、导出、右键和双击进入，不能再按早期 dormant 记录认定缺失。 |
| 代理、离线重连、收集归档、按使用区段 consolidate | `src/shared/media/asset_proxy_manager.py:86`；`src/features/edit/ui/relink_media_dialog.py:335`、`:366`、`:413`；`src/features/edit/core/project_archive.py:160`；`src/features/edit/core/consolidate.py:429`。导出默认 master：`src/features/edit/core/proxy_policy.py:166`。 |
| BWF、D/M/E/Bus stems、LUFS、路由/总线/sidechain、ADR | `src/features/edit/core/interchange.py:353`；`src/features/edit/core/stem_export.py`；`src/features/edit/core/loudness_meter.py`；`src/features/edit/core/audio_graph_renderer.py`；`src/features/edit/ui/adr_take_sheet_panel.py:97`。 |
| 字幕多格式与样式/QC/交付 | `src/features/edit/core/captions/export.py:20` 已支持 SRT/VTT/ASS/TTML/DFXP/SCC；`src/features/edit/core/captions/importer.py:13` 已支持 SRT/VTT/ASS/SSA/TTML/DFXP/XML。不能泛报“缺少 TTML/SCC”。 |
| FCPXML / OTIO | `src/features/edit/core/capability_registry.py:257` 标记 PARTIAL 并记录 File 菜单生产入口；两个 adapters 实际存在。OTIO 依赖是可选的，当前环境是否安装需主代理核实。 |
| 自动保存、恢复文件与备份恢复 UI | `src/features/edit/core/edit_project_io.py:612`、`:643`、`:685`；`src/shared/project/topos_project_io.py:419`、`:749`；`src/app/ui/project_manager_dialog.py:890`、`:943`。不能直接报“缺少自动保存/恢复”。 |

## 重要专业差距与优化计划表

| ID | 建议优先级 | 能力/价值 | 当前状态、仓库证据与判断边界 | 建议实现及验收 | 官方对标 |
|---|---|---|---|---|---|
| G01 | P1 | Source Patching：逐源视频/音频流选择与映射 | **部分支持**。独立 V/A 目标轨已做；`timeline_view.py:16720` 仅按 asset.audio_channels 判断是否整体插入 linked A/V，UI 无逐源声道 source patch 开关/映射表。此结论是所查入口的代码确认。 | 从“选择一个目标音轨”扩展为源流/声道→目标轨映射、视频/音频独立开关。验收：双系统录音或多声道素材只送选中声道至指定 A1/A3；video-only/audio-only Insert/Overwrite 不产生意外其他流；单次 undo 完整恢复。 | [Adobe Source Patching](https://helpx.adobe.com/premiere/desktop/edit-projects/intro-to-editing/add-media-to-the-timeline-using-source-patching.html) |
| G02 | P1 | 完整三点变体：backtime、目的范围定长剪辑、四点时长冲突处理 | **基本三点已有，高级变体未找到生产入口**。`edit_page.py:2797` 强制源 In/Out 都存在，`timeline_view.py:16653` 使用 playhead 作为起点；未找到 backtime/Fit-to-Fill/four-point 命令。 | 增加目的 Out 对齐与目的范围优先的规则，四点冲突给出 trim source / change speed 等明确选择。验收：素材恰好结束于音乐重拍；只给源 In+目的 In/Out 可生成定长片段；所有模式混帧率时帧级可预测。 | [Apple 三点与 backtime 示例](https://support.apple.com/en-ca/guide/final-cut-pro/ver549f253e/mac) |
| G03 | P1 | 真正的 A/B Trim Viewer、循环监听与 JKL 动态修剪 | **部分支持**。已有基础修剪与数值 trim，但 `trim_numeric_dialog.py:156` 的 TrimMonitorDialog 只创建两个时间码 QLabel、Δ、headroom；`timeline_view.py:4210` 只调用 update_roll，没有 A/B 图像输入。未找到 JKL 动态 trim。 | 显示 cut 两侧真实帧与边界预览，加入预卷/后卷循环、JKL 修剪与 commit/cancel。验收：Roll ±1f 时 A/B 实际图像与最终导出对应；播放期间 trim 不丢焦点，Esc 回退整个事务。 | [Resolve 专业修剪](https://www.blackmagicdesign.com/products/davinciresolve/edit/)；[Adobe Trim 模式文档入口](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html) 的 Trim clips 章节 |
| G04 | P1 | 多机位同步实时监看与独立音频跟随策略 | **部分支持**。`multicam_panel.py:149` 每 250ms 合并请求，`:319` 向 ThumbnailProvider 请求异步缩略图，不是多路实时视频监看；切角 `edit_controller.py:7325` / `commands.py:2945` 只对选中视频片段换源，无公开 Audio-Follows-Video / Audio-only / Video-only 模式字段。 | 把缩略图网格升级为共享播放时钟的多路解码显示；补独立 AV 切换策略。验收：4/9 路活动机位同步画面、连续切角记录准确；锁定采访主音轨后切视频不改声音，启用 audio-follow 时符合选定策略。 | [Resolve Multicam](https://www.blackmagicdesign.com/products/davinciresolve/edit/) |
| G05 | P1 | 多种多机位同步方法及长录制漂移修正 | **部分支持**。当前生产 worker `multicam_sync.py:250` 默认取开头60秒、搜索±5秒，输出每机位一个固定整数帧 offset；UI只有波形同步。`ai_editing/editorial_advanced.py:64` 虽列 timecode/face/slate 枚举，但为 evidence dataclass，不能视为这些同步引擎已接线。 | 提供 embedded timecode / In point / marker 的同步入口、可调搜索与选区；长录制检测起末漂移并给出校准。验收：无有效开头音频仍可按 TC/In 点同步；30–60分钟独立时钟录音有漂移报告，失败不写错误 offset。 | [Resolve Multicam 同步选项](https://www.blackmagicdesign.com/products/davinciresolve/edit/) |
| G06 | P1 | 实时保调变速，预览与导出听感一致 | **缺实时部分，已披露边界**。`audio_playback.py:1400` 明确走 resample 变调；`edit_properties_panel.py:4019` 已写 Preserve Pitch (export only)。第五轮已修复的是披露，未完成实时 DSP。 | 将带状态的时间伸缩流接入既有 AudioGraphRenderer，处理 seek、loop、scrub 后重置。验收：0.5×/1.25×/2× 语音预览与导出音高、时长一致；切点不爆音，低延迟不阻塞 UI。 | [Apple Preserve Pitch](https://support.apple.com/en-ae/guide/final-cut-pro/verb6aca45d/mac) |
| G07 | P1 | 音频子帧/采样精度的剪辑、偏移、fade | **模型与 UI 主要受整数帧限制**。`timeline_v2.py:232` 的 timeline_start/duration/source_start/duration 都是 int 帧，fade 也为帧；未找到单独 sample offset。注意：`audio_playback.py:941` 已有 sample-accurate decoder seek，这不等于用户能以1 sample摆放/修剪片段。 | 增加音频样本时域或有理时间偏移，与视频帧保持清晰映射。验收：48kHz音频可移动1 sample，保存重开和 stems 输出保留该偏移；视频仍帧对齐；undo 精确恢复。 | [Adobe 音频时间单位](https://helpx.adobe.com/premiere/desktop/organize-media/apply-labeling/change-timecode-display-format.html) |
| G08 | P1/P2 | AAF 到 Pro Tools / Media Composer 的混音交接 | **明确缺失**。`interchange.py:392`、`capability_registry.py:296` 标 unavailable；已有 BWF/stems/CSV 只能作为阶段替代。OMF旧格式可继续明确不做，优先AAF。 | 支持轨/片段位置、源时码、音量/关键帧、handles、分离音频或嵌入、单声道拆分；不支持效果给报告。验收：Pro Tools 与至少一个 NLE 真实导入，逐片段起止/声道/handle正确，交付manifest标明已bake内容。 | [Adobe AAF 导出](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-aaf-files.html) |
| G09 | P2 | FCPXML/OTIO 专业往返覆盖与重 conform | **已有受控子集，非全功能交换**。`fcpxml_adapter.py:18` 明确降级任意嵌套、复杂速度曲线、multicam/audition、完整调色、音频总线/自动化；`capability_registry.py:257` 正确标 PARTIAL。 | 按实际用户交换场景扩展映射，并建立外部软件黄金工程；跨版本读取/回写保留可重建信息。验收：Resolve/FCP/Premiere各一条混帧率、字幕、变速、转场、嵌套时间线往返对照，失真逐项报告；不要求“一次支持所有”。 | [Adobe AAF互操作与明确限制示例](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-aaf-files.html)；本项具体适配目标为工程建议 |
| G10 | P2 | 文稿直接剪视频、删除停顿/口头词的用户工作区 | **分析/字幕/AI计划基础已有，直接编辑闭环未找到**。`editorial_advanced.py:27` 有 TranscriptRevision 和 silence/filler 元数据，但这些类型在 UI/控制器未找到生产消费者；已有 transcript provider 与 AI剪辑能力不应称为“没有转录”。 | 新增词级时间映射文稿视图，区分修改文字和删除媒体；所有切除走现有命令事务。验收：选择一句并 ripple delete 同步变更 linked A/V、字幕、markers；撤销恢复；变速/多段源映射与重新转录不丢用户修订。 | [Adobe Text-Based Editing](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html) |
| G11 | P2 | 镜头候选版本/Audition 比较 | **未找到通用视频候选版本容器/入口**。FCPXML adapter把 audition 列不支持；ADR 的 TakeAuditionPlayer 与 AI 音乐试听是专项音频能力，不能代替一个时间线镜头位置的多备选版本。 | 可在同一位置保存多 take/效果版本并切换比较，选择后仍可回看候选。验收：三种候选不同长度、音频联动可预期；保存重开保留、可单次撤销、交换时明确 flatten。 | [Apple Auditions](https://support.apple.com/guide/final-cut-pro/intro-to-auditions-verbbd3587d/mac) |
| G12 | P2 | 多人工程/审片评论/命名版本 | **未发现多人工程会话、冲突解决、外部审片评论回流入口**；当前工程模型 revision 是AI乐观锁计数，不能等同团队版本历史。字幕 cue 自带 assignee/note 也不能等同多人项目。自动保存和本地备份已存在。 | 先做命名快照与只读审片链接/评论→marker，再评估共享项目锁/乐观合并。验收：两人编辑同一版本有明确冲突，单人离线编辑不会覆盖同伴；评论帧位、作者、状态可回流且可审计。 | [Resolve Collaboration](https://www.blackmagicdesign.com/products/davinciresolve/collaboration/)；[Adobe Auto Save History](https://helpx.adobe.com/premiere/desktop/collaborate-with-others/collaborate-using-team-projects/view-auto-saves.html) |
| G13 | P2 | 批量素材属性修改 | **明确缺失，已披露边界**。`edit_properties_panel.py:2088` 明确 batch editing not supported。时间线多选/批量效果若已支持，应与“媒体池素材批量属性”分开。 | 批量设置共用元数据、解释帧率/声道映射、色彩标记、标签等，有混合值显示与失败汇总。验收：选20素材一次提交、失败零半提交或明确逐项结果、可撤销并持久化。 | [Apple 浏览器组织/元数据基础](https://support.apple.com/guide/final-cut-pro/intro-to-auditions-verbbd3587d/mac) 的 Organize your library 章节；需求优先级为工程判断 |
| G14 | P2 | 通用图像序列交付 | **明确缺 PNG/TIFF 导出，已披露边界**。`timeline_export.py:575` 配置层拒绝 PNG_SEQUENCE/TIFF_SEQUENCE，唯一实现为 Topos Image Sequence；`video_export_formats.py:53` 仍有类型定义。 | 根据用户交付优先支持 PNG/TIFF，再考虑 EXR/DPX；清晰设 alpha、bit depth、起始帧号、色彩、缺帧与中断续写。验收：外部合成软件按正确帧率重新导入，帧数/命名/alpha/色彩对上；取消不伪装完成。 | [Adobe 支持导入的图像/序列及格式](https://helpx.adobe.com/pdf/mediaencoder_reference.pdf)；序列导出实现范围为工程建议 |
| G15 | P2/P3 | 第三方音频插件宿主 | **内置 DSP 已丰富，未发现 VST3/AU 扫描、宿主、插件状态序列化与崩溃隔离**。`audio_dsp.py:2361` 的 registry 是内置 AudioEffect 类映射；GLSL 插件系统不能作为 VST/AU 宿主。 | 只有在现有音频实时性稳定后再做 VST3/AU；先定义通道/延迟补偿/离线渲染/状态兼容契约。验收：一个 EQ、一个有lookahead插件的预览/导出一致；缺插件明确 offline/降级，不静默丢效果；插件崩溃不丢工程。 | [Adobe 音频第三方效果交换](https://helpx.adobe.com/fr/premiere/desktop/use-premiere-with-other-apps/working-with-other-adobe-applications/transfer-of-information-from-premiere-to-audition.html) |

## 应纳入主报告的复验门槛，而不是直接记为“缺失”

- **代理离线/回原片**：已有系统，需真实移动master、只保留proxy、再重连、导出强制master各走一遍，核对帧位/颜色/音频，不仅看生成成功。
- **自动保存/恢复**：已有系统，需用独立工程测试未命名工程、后台保存与手动保存竞争、崩溃后恢复和选择旧备份；不能宣称当前缺失。
- **字幕广播交付**：TTML/SCC输出实现已存在；需外部播放器/广播工具验证CEA-608字符集、奇偶校验、pop-on时序、语言/服务、帧率/DF、QC报告。不能仅因文件成功生成就宣称达到广播合规，也不能未经验证断言不合规。
- **HDR/色彩与音频交付**：已有Metal/CPU/GPU路径、BWF、stems；应实际比对预览/导出/回导，与缺功能分别记录。
- **Match Frame**：第五轮 E09 已做双击片段打开源并跳帧，但尚未看到按F匹配/反向匹配/按目标轨选上层的独立命令。建议先测现有双击在变速和混帧率下映射再定义扩展，不计“完全没有源匹配”。

## 一个源码/能力声明一致性问题（低优先级）

`src/features/edit/core/interchange.py:381` 仍写 FCPXML available=False、建议下一迭代实现，`tests/edit/test_interchange.py:359` 仍断言False；与此同时 `capability_registry.py:257` 与实际 File 菜单/adapter 已宣称 PARTIAL。当前检索没有发现该旧表直接关闭生产 FCPXML UI（新registry为手写PARTIAL），因此只定为 **P3 能力表/测试过时**，不要夸大成“FCPXML不可用”。建议统一权威能力源，并让旧helper与新registry描述一致；验收为相关能力查询都返回“受控子集可用”，且OTIO依赖状态准确。

## 官方依据取用范围

官方页面于2026-10-21联网核查。只借用功能定义，不把商业产品宣传中的速度/质量承诺视作本项目基准。Resolve Edit当前页面标Resolve21；Apple当前指南可选多个版本。各项优先级、Topos代码判断及验收方案均为本次工程推断；不是官方对Topos的评价。未使用社区/第三方博客作为技术结论来源。
