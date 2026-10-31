# F0.2 · 当前能力盘点与差量清单（evidence ledger）

> 审计日期：2026-10-22；基线 commit：`bde78291b`（feat/f-plan）。
> 方法：5 路并行只读审计（能力注册表+音频 / 剪辑 / 调色色彩 / 媒体交接交付 / 合成版本AI），全部结论带 file:line 证据；「判断」分三档：完整闭环 / 部分 / 仅注册项或计划对象。
> 本 ledger 是 F1.3（能力声明一致性）与 F2–F9 各任务「核验现实现」步骤的权威输入。

## 0. 总体判断分布

| 域 | 完整闭环（真实生产链） | 部分实现 | 仅注册项/计划对象 |
|---|---|---|---|
| 注册表+音频 | 音频图快照门/渲染导出三路径同源、retime/保调导出、loudness+true-peak（取消三处齐备）、ADR 录音、Stem、嵌套音频 flatten、BWF | 实时保调（仅离线 WSOLA） | CSV Cue Sheet（无生产入口） |
| 剪辑 | 数值 trim/步进/标记对齐、A/V 目标轨路由、Transform 动画（值域已修+迁移）、恒速/速度点/smooth/freeze、内置标题+图形绑定生命周期 | Source Patching（单 V/单 A，无多声道映射）、三点基础、多机位波形同步+切角、变速插值 | backtime/fit-to-fill、真双画面 Trim、optical_flow、用户模板管理 UI |
| 调色色彩 | 二级工具全套（Qualifier/curves/warper/PowerWindow+跟踪绑定）、grade/mask 关键帧白名单（预览=导出）、scope（HDR 单位标尺）、跟踪双端、素材级共享调色 | still/Gallery（8-bit JPEG）、RAW（仅静图）、CM（导出闭环/预览割裂） | 场景组调色、命名版本、HDR 分区、difference 对比 |
| 媒体交接交付 | FCPXML 受控子集往返、离线检查/重连、归档拷贝+SHA-256、DiskProxyService、交付队列（崩溃恢复/重试）、preflight、字幕导入导出/QC/burn-in、PNG/TIFF+Topos 序列输出 | 代理（无配额/重试入口）、输入解释（探测有/消费缺） | AAF/OMF、OTIO 生产路径（依赖未声明+恒 MissingReference）、SIDECAR 执行、mux |
| 合成版本AI | Composite/Color 跟踪、keyer、视频去噪（预览=导出）、双层节点缓存、take sheet、AI 编辑主链路（转录/翻译/审批/单undo/检查点） | RIP bake（无 UI）、ML Roto（无 UI）、人脸跟踪（检测器未注入）、工程快照（仅时间戳备份） | ShotMatchWorkflow、ai_denoise 占位节点、审片评论互导 |

## 1. 能力注册表审计（F1.3 核心输入）

注册表 `src/features/edit/core/capability_registry.py`（12 条目）**唯一生产消费者是导出 manifest**（export_manifest.py:400-403）；UI 帮助/AI 走另一套 `ai_editing/capability_manifest.py`（零引用本注册表）。「单一事实来源」自述未兑现。

| # | 矛盾/缺口 | 证据 | 处置 |
|---|---|---|---|
| R1 | FCPXML 状态漂移：静态表 available=True vs registry PARTIAL，`find_status_drift()` 每次产出漂移，测试反而断言静态表必须 True | interchange.py:388 vs capability_registry.py:259；test_interchange.py:362 | F1.3 修 |
| R2 | 静态能力表（INTERCHANGE_CAPABILITIES 等）零生产消费 | interchange.py:353-434 全库仅 drift 检测+测试 | F1.3 处置 |
| R3 | interchange.py DORMANT 头注释过期（时间码 6 处生产调用+File 菜单入口） | interchange.py:1 | F1.3 修文案 |
| R4 | CSV Cue Sheet 状态虚高：AVAILABLE 但 `build_cue_sheet_from_sequence` 无生产入口 | capability_registry.py:231-239；interchange.py:287 | F1.3 改 PARTIAL/DORMANT 或补入口 |
| R5 | DORMANT 枚举零使用；休眠模块（audio_health、edit_frame_coordinator、4 个 AI 面板）不在注册表 | capability_registry.py:43 | F1.3 扩表 |
| R6 | loudness/录音ADR/音频图门/audio_capabilities 探测均不在注册表 | 各域闭环证据见 §2 | F1.3 扩表 |
| R7 | AI 侧能力对 AI 只通过导出 manifest 间接可见 | host_help_sources.py:151 | F1.3 记录 |

## 2. 逐域能力矩阵（浓缩；完整 file:line 见各任务开工时按本 ledger 索引复查）

### 2.1 音频（F3 输入）
- **闭环**：audio_graph_snapshot（编辑/导出双门，edit_page.py:1100、timeline_export.py:2876）；AudioGraphRenderer 导出侧（timeline_export.py:4075、stem_export.py:268）；audio_retime 三路径同源（ClipRetimeReader audio_retime.py:279）；loudness BS.1770-4/R128 + 4x 过采样 true-peak + 取消（loudness_analyzer.py:42/220/247，timeline_export.py:4444）；RecordingManager/ADR（recording_manager.py:768，take 单步 Undo import）；Stem（stem_export.py:736）；嵌套 flatten 纯函数三路径共用（nested_sequence.py:704）。
- **缺口**：实时保调不存在（audio_playback.py:920-924 resample 变调，UI tooltip 兜底"导出保调/预览变调"）→ F3.2；父片段 retime 时子音频整体跳过（nested_sequence.py:789-792，diagnostics 记录）→ F3.1 嵌套语义；采样级编辑模型未见（F3.3 从零建）。

### 2.2 剪辑（F2 输入）
- **闭环**：V/A 目标轨+linked 插入/覆盖/append（timeline_view.py:16746-17157）；数值 trim/Ripple/Roll/Slip/Slide+标记对齐+手柄余量（trim_numeric_dialog.py:23-154，test_trim_professional.py）；Transform/opacity 关键帧含负位移/大角度值域修复与旧工程迁移（clip_params.py:249-258，timeline_v2.py:207-230，test_fix_round7_r11_legacy_ranges.py）；恒速/速度点/smooth/freeze/rate-stretch（clip_params.py:448-523/752-868）；标题内置模板+CaptionGraphicBinding 提升/脱离/降级/复制（captions/graphics.py:23-146/589-610）；多机位波形同步（60s 头段互相关+置信度，multicam_sync.py:236-304）+切角命令+网格取帧一致（multicam.py:276-322）。
- **缺口**：源多声道→目标轨映射、V/A patch 开关 → F2.1；backtime/目的 Out 倒推/fit-to-fill/四点冲突（全仓零实现）→ F2.2；Trim Monitor 仅 TC 文本双栏非真双画面（trim_numeric_dialog.py:157-221）、JKL 仅普通播放、无预/后卷循环 → F2.3；`SetKeyframeInterpolationCommand` 无 UI 消费者（commands.py:4385）、时间线无关键帧可视化 → F2.4；多机位仅波形一种同步、单一常量 offset、仅前 60s → F2.6；optical_flow 仅枚举（clip_params.py:506，采样退化为单帧 :958-963，UI 隐藏 :4126，composite retime_node.py:14 RESERVED）→ F2.8；用户模板 save/delete API 零生产调用（title_templates.py:102-160）→ F2.9；多机位无声钟/音频跟随策略 → F2.5。

### 2.3 调色/色彩（F4/F5 输入）
- **闭环**：二级工具全套（layer.py:33-548 + composition_uniforms.py:252-282 合成链顺序：蒙版→qualifier→grading→curves→mixer→warper→LUT→grain→插件）；grade/mask 关键帧白名单 15+9 参数、预览=导出同一求值（grade_keyframes.py:34-50/367-377，timeline_export.py:8996 vs image_engine.py:1667）；scope 完整含 percent/nits(PQ/HLG)/code10 标尺+falsecolor+zebra（scope_types.py:31-63，scope_panel.py:509-523）；跟踪（shared/tracking 双端+手动修正+失败检测+schema 迁移，12+10 测试文件）；素材级共享调色 UseSharedGradeCommand+asset_default 栈（color_stack.py:31-106，commands.py:16677）；wipe/split A/B 对比（image_engine.py:1373-1472）。
- **缺口**：场景/机位组+组前后处理顺序（grep 零命中，scope 仅 clip_local/asset_default）→ F4.1；命名版本/活动版本指针/一键比较（color_session.py 无 version 字段、grade_clipboard 单槽）→ F4.2；still 8-bit JPEG 丢超 1.0/负值（preset_meta.py:138-145）、无 difference 模式 → F4.3；qualifier/curve/warper/LUT/effects 不可动画（白名单外显式跳过 grade_keyframes.py:338）→ F4.5；builtin.hdr 是面板布局非分区能力（workspace_presets.py:93-108）→ F4.6；预览/导出显示变换不共用 provider（Edit 预览内联 sRGB OETF timeline_preview_renderer.py:3519-3535、Color 页自有 shader、仅 Composite 走 provider page_controller.py:1020-1083）→ F5.1；RAW 面板仅静图（raw_panel.py:438 set_enabled_for_file）→ F5.3；PNG/TIFF 连号序列无输入入口（factory.py:233-246 硬编码 .toos）、VFR 无帧号索引（gop_index.py:22-24 CFR 假设）、rotation 探测无消费（ffmpeg_source.py:937-960）→ F5.4；监看 8-bit uyvy422 固定+显示变换在上游（monitor_output.py:52）→ F5.2（设备/B 延期，仅记录）。

### 2.4 媒体/交接/交付（F6 输入）
- **闭环**：FCPXML 受控子集导入导出+四类报告+防护（fcpxml_adapter.py:173-213/324/677，interchange_types.py:28-81）；ImportBuilder 落地（去重/离线降级，import_builder.py:155-307）；离线检查+重连对话框（offline_asset_manager.py:194/333，relink_media_dialog.py:51）；归档拷贝+SHA-256+目录聚合哈希+缺失素材使用位置（project_archive.py:160）；DiskProxyService 生成/取消/暂停/恢复/清理（disk_proxy_service.py:161，全局消费）；代理策略可撤销命令（commands.py:6206）；交付队列（崩溃 RUNNING→PENDING、重复 id 拒绝、retry，export_queue.py:282）；preflight 分级（video_export_preflight.py:197/363-731）；字幕导入 SRT/VTT/ASS/SSA/TTML/DFXP+导出 6 格式+序列级交付 manifest+burn-in 字体预检（captions/importer.py、export.py、delivery.py:164）；PNG/TIFF 序列导出已有（image_sequence_encoder.py:55-59）+Topos 序列输入输出双向（topos_image_sequence.py:206、topos_encoder.py:3630）；Smart Bin/关键词/评级/批量日志（project.py:600/4634，BatchUpdateAssetLogCommand）。
- **缺口**：OTIO 依赖未声明 requirements（grep 0）+导出不传 asset_paths 恒 MissingReference（FC:7480 附近 vs otio_adapter.py:288-329）→ F6.1；交换导入不走 command/undo → F6.1；重连仅 basename 匹配无 fingerprint/时码/多候选（offline_asset_manager.py:462/490-503）→ F6.2；归档不收集 LUT/字体 → F6.2；AAF 无 adapter（声明 UNAVAILABLE 属实）→ F6.3 新增受控能力；**F6.4 输出侧已基本具备（PNG/TIFF 序列导出在 image_sequence_encoder）**，差量为恢复/确认与外部回导验证；导入去重仅路径无内容指纹 → F6.5；代理无配额/年龄清理、失败重试无显式入口、AssetProxyManager 休眠双轨（DORMANT 头注，生产用 DiskProxyServiceAdapter source_resolver.py:101-140）→ F6.6；D3D11 导出 fail-closed 回 CPU（timeline_export.py:7703-7712，B 侧）；字幕 SIDECAR 仅校验无执行、mux.py 无生产调用 → F9.3/F6 交付补强。

### 2.5 合成/版本/AI（F7/F8/F9 输入）
- **闭环**：Composite+Color 跟踪（点/TRS/平面、置信度、手动修正、分段重跟、bake、导出互通 facade.py:194）；keyer 家族+matte 后处理链；视频去噪预览=导出同管线+时域状态机重置规则全（video_temporal_denoise.py:34，export_denoise_bridge.py:66）；双层缓存（内容寻址 GPU cache_manager.py:107 + A6 磁盘缓存+后台写 bounded）；take sheet 完整（take_sheet.py:95）；AI 编辑主链路（7 工作流、转录本地/远程 profile、翻译注入式、审批+单 Undo 提交、checkpoint+断点恢复、商用导出门）；字幕 QC（行长/阅读速率 CJK 感知/重叠，captions/quality.py:72）；交付 QC（离线/时长/字幕越界/fps + 文件级 SHA-256，delivery_qc.py）。
- **缺口**：人脸检测器未注入（face_track_node.py:36 set_face_detector 零调用，恒 NEEDS_DETECTOR）→ F7.1；ML Roto 算法完整无 UI 入口（ml_roto.py:197）→ F7.1；RIFE 模型 source_url="" 默认不可得、线性混合降级（model_manager.py:128-132，frame_interpolate_node.py:363-370）；RIFE 未接 Edit 变速 → F2.8/F7.2；RIP bake 服务+测试完整无 UI 菜单（bake_service.py:1）→ F7.3；命名工程快照/版本比较不存在（仅时间戳滚动备份 project_manager.py:689 + 自动保存）→ F8.1；审片评论互导缺失、marker 无作者/时间戳/状态字段（timeline_v2.py:151-170）→ F8.2；黑场/静帧/缺帧像素级 QC 无（delivery_qc.py check 列表）→ F9.3；ShotMatchWorkflow 仅计划对象（color_context.py:346-388 零消费者）→ F4.4；quality_gate 仅离线 eval 消费（eval_runner.py:105）→ F9.2 补强点。

## 3. 四桶分类汇总（供各任务按图索骥）

| 桶 | 条目 |
|---|---|
| **已有能力覆盖**（后续任务按场景验收后关闭，不从零建） | 音频图/响度/录音/Stem/嵌套 flatten；数值 trim；A/V 路由；Transform 动画；恒速变速+freeze；内置标题+图形绑定；多机位波形同步+切角；二级工具；grade/mask 关键帧；scope；跟踪；素材共享调色；wipe A/B；FCPXML 子集；离线/重连/归档；DiskProxy；交付队列+preflight；字幕全链+QC；PNG/TIFF/Topos 序列输出；Smart Bin/批量日志；keyer/去噪/节点缓存；take sheet；AI 主链路 |
| **源码确认缺口**（按 F 任务实施） | F2.1 多声道映射/patch 开关；F2.2 backtime/fit-to-fill；F2.3 真双画面+JKL+循环；F2.4 曲线 UI；F2.5 音频切换策略；F2.6 TC/marker 同步+漂移；F2.8 光流接通或明示；F2.9 模板管理 UI；F3.2 实时保调；F3.3 采样级模型；F4.1 场景组；F4.2 版本；F4.3 HDR still+difference；F4.5 qualifier 动画；F5.1 provider 统一；F5.4 序列输入/VFR/rotation；F6.1 OTIO 声明+asset_paths+undo；F6.2 fingerprint 重连+归档依赖；F6.3 AAF；F6.5 指纹去重；F6.6 配额/重试；F7.1 人脸注入+ML Roto 入口；F7.3 RIP UI；F8.1 快照；F8.2 评论；F9.3 黑场/静帧/缺帧 QC |
| **仅建议补强**（F1.3 统一处理） | §1 的 R1–R7；interchange 头注；SIDECAR 执行/mux 接线或降级声明；AssetProxyManager 双轨收敛声明 |
| **需复测**（F1.1 重放 26 项历史） | C01–C06/D01–D10/U01–U07/Q01–Q03（多数在近 7 轮修复中已有 focused test，F1.1 逐项重放并补缺口证据） |

## 4. 对 F0.1 阻塞映射的反馈

审计未发现需要修改 F0.1 §3 处置口径的新事实：所有「可推进」任务的缺口均为 Mac 可实施；DeckLink/Windows 相关缺口（D3D11 fail-closed、监看 8-bit）与 F0.1 登记一致。

## 5. 追加（2026-10-22，F0.3 G 级驱动实测发现）

- **R8**：`EditPage.create_timeline()` 的 `timeline_created` 信号全 src 无 connect（edit_page.py:120 声明、:2595 发射）——死发射；真实创建路径为媒体池 `timeline_create_requested` → `edit_controller._on_timeline_create_requested`（:1012）。处置：F1.3 清理或接线。
- **R9**：跨序列粘贴按原 track_id 匹配目标轨，序列轨道 id 不同则条目被跳过（真实窗口实测 pasted=0）——「跨时间线复制保持身份/时间基/链接」差量，登记 F2 任务（建议 F2.1 随源/目的轨映射一并处置）。证据：docs/validation/feature_F/F0.3/c2c0a841c/run_2026-10-22/report.json G4。
