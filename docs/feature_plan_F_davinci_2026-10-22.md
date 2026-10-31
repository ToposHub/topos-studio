# 计划表 F · 专业功能与工作流实施（完成 A/B 性能计划后执行）

> 创建：2026-10-22；编制时源码 HEAD：`816a2d84f`。这不是开始实施时的固定版本，开工须重新核验。
> 技术栈：PyQt6 + ModernGL + GLSL，含既有 C ABI、Metal、Windows GPU 契约。
> 版本：v2（第二轮遗漏复核）；状态：**计划已编制，功能任务全部未开始；本次没有执行产品代码改造。**
> 编号使用 F（功能），避免与 B 计划中 C 农场编号混淆。

配套：[性能计划 A v3](perf_plan_A_mac_2026-10-22.md)、[性能计划 B v3](perf_plan_B_windows_2026-10-22.md)、[功能评估](davinci_functional_gap_review_2026-10-22.md)、[旧交互审计](edit_page_sixth_full_audit_optimization_plan_2026-10-21.md)。旧审计只提供历史复现材料，不能直接代表未来开工时仍有这些缺陷。

## 0. 目标、接续条件与完成定义

本计划把“可靠编辑、成片调色、声音处理、外部交接和交付”拆为可验证工作包。**先完成并收口 A/B，再启动 F；不并行启动本计划的功能实施。** 编制计划不代表承诺已经达到 DaVinci Resolve 的全面功能、画质或性能水平。

### 0.1 A/B 接续门禁

F0.1 开工前，A/B 各提交：最终 commit/构建指纹、已验证设备/格式/效果矩阵、原始基准及回归阈值、遥测 schema、资源/取消/关闭契约、已知限制和未启用开关清单。

A/B 中有条件的 native/RHI/Metal/D3D11/DeckLink 项须标明“已验收 / 评估不适用 / 已有能力覆盖 / 显式延期且隔离”。后几种不等于功能实现完成，也不允许进入产品能力声明。普通 OpenGL/CPU 回退稳定、所需接口可用时，可继续不依赖延期项的 F 任务；依赖真 GPU/监看/扩展的任务仍保留阻塞。**不能为了等所有可选重构而无限阻塞核心功能，也不能将未做的项目算作完成。**

### 0.2 每项任务统一完成标准

- 状态流转：未开始→核验中→实施中→待验收→已验收；另有“已有能力覆盖”“条件未满足/延期”。只有有证据的“已验收”或“已有能力覆盖”计入核心完成，延期单列。
- 先核现实现/调用者/测试，再补最小缺口；旧 bug 不复现时只补缺少的回归证据，不重复改代码。
- 同时交付用户入口、领域提交、Undo/Redo、持久化、预览/导出、能力/诊断、失败恢复与测试。纯类定义、registry 项或面板不算端到端完成。
- `.topos`/子 schema 变更先写版本、迁移、旧文件兼容和未知 payload 保留测试。新字段不保存 secret、机器绝对素材路径或大型可重建缓存。
- 模型变更走既有 command/service/transaction。UI 不直接修改 renderer/序列化对象；状态锁防信号递归；GPU 资源按渲染线程与 deferred release 契约创建/回收。
- 低风险文案修正不强制新增测试；渲染、音频、线程、序列化、交接的行为变更必须有 focused test 和实际宿主/媒体验证。
- 实施前确定本任务的主要指标、允许退化范围和数值容差，继承 A/B 同机测量协议。实际支持格式不能用 silent fallback 冒充 GPU 或无损支持；skip/缺设备不算真机通过。
- 每次只改一个可审查范围。保留用户工作区改动；不把 pull、checkout、重建环境、rebase 或发布列成自动前置动作。

### 0.3 证据与归档

S＝源码；T＝逻辑/替身测试；G＝真实应用窗口和生产入口；R＝实际媒体渲染/解码/PCM；H＝对应 GPU/音频/监看设备；X＝外部接收软件。任务按适用等级验收，不能以 T 代替 G/H/X。

建议产物根为 `docs/validation/feature_F/<任务ID>/<commit>/<run_id>/`（**拟新增**）。小型摘要/manifest 可入库；大媒体和日志由证据 manifest 指向受控产物位置，不全塞 Git。每包记录输入素材/工程 hash、环境、操作或事件序列、结果、返回码、容差、基线、未验证项和复现命令。合成精确向量与真实素材分别标识；截图只辅助交互，不能代替数据/画质/音频检验。

## 1. 执行顺序与可勾选总表

推荐先完成 **F0→F1 的核心门禁→F3.1 音频语义与 F5.1 色彩闭环**，再按产品优先级推进剪辑/声音、调色、媒体交接、合成与交付。Phase 是工作分类，**编号不表示必须整阶段串行完成**；F1.4 的硬件子项也不阻塞普通功能。F10 为条件扩展，必须显式立项。独立纯读/测试整理可并行，共享模型、schema、时间基、颜色 ABI、command 注册表由一个工作包串行修改。若先交付 Color 专注版，可优先 F4/F5，但仍遵循逐项硬依赖；不能跳过核心发布门禁。

优先级：P0＝接续/正确性基础；P1＝专业工作流核心；P2＝效率与扩大覆盖；P3＝条件扩展。优先级不替代依赖。**硬依赖**决定何时可启动；**条件依赖**只阻塞相应新增子能力，未验证的子能力仍须保持部分/未验收。不能把无关 AI/新 schema 当作现有字幕、交付、重连或色彩验证的前置。

| 完成 | 任务 | 优先级 | 硬依赖 | 条件依赖（仅对应子项） | 初始状态 |
|---|---|---|---|---|---|
| ☑ | [F0.1 A/B 收口与开工基线](#f0-1) | P0 | A/B 接续门禁 | — | 已完成（A `2d7f18e0f` + B `0b57c322d` 双侧收口，B 经合并 `4c22cacf5` 入 fix-ui；终版 manifest 替换旧阻塞映射，[manifest](validation/feature_F/F0.1/0b57c322d/manifest_2026-10-24.md)） |
| ☑ | [F0.2 当前能力盘点与差量清单](#f0-2) | P0 | F0.1 | — | 已完成（[capability ledger](validation/feature_F/F0.2/bde78291b/capability_ledger_2026-10-22.md)） |
| ☑ | [F0.3 黄金工程、媒体与验证驱动](#f0-3) | P0 | F0.2 | — | 已完成（T 12/12 + G 13/13，[manifest](validation/feature_F/F0.3/c2c0a841c/manifest_2026-10-22.md)） |
| ☑ | [F0.4 时间、色彩、身份与迁移契约](#f0-4) | P0 | F0.2 | — | 已完成（ADR 冻结+43 精确向量，[ADR](validation/feature_F/F0.4/ef91445f2/adr_timebase_identity_migration_2026-10-22.md)） |
| ☑ | [F1.1 历史缺陷复测与跨页闭环](#f1-1) | P0 | F0.3、F0.4 | — | 已完成（191+10 重放/补齐测试，D03 修复，U06 真实窗口，Q01 五轮收敛+残项移交 F1.2，[ledger](validation/feature_F/F1.1/d2833a069/replay_ledger_2026-10-22.md)） |
| ☑ | [F1.2 工程与任务恢复的故障路径](#f1-2) | P0 | F0.3、F0.4 | — | 已完成（保存/恢复注入 6 用例 + Q01 四层根治 + 恢复入口明确，[ledger](validation/feature_F/F1.2/4811a68fe/fault_injection_ledger_2026-10-23.md)） |
| ☑ | [F1.3 统一能力声明、提示与测试分层](#f1-3) | P1 | F0.2、F1.1 | — | 已完成（R1-R9 全处置+零漂移断言+基线 present 缺陷修复，[ledger](validation/feature_F/F1.3/0023acb4c/capability_consistency_ledger_2026-10-23.md)） |
| ☑ | [F1.4 快捷键、工作区与硬件控制的效率验收](#f1-4) | P2 | F1.1、F1.3 | F4.5（见正文） | 已完成（JKL 文本屏蔽缺陷修复+Tangent 手势边界/rebase+副屏回落具名用例；H 级未验证项如实登记，[ledger](validation/feature_F/F1.4/a50417ec2/efficiency_acceptance_ledger_2026-10-23.md)） |
| ☑ | [F2.1 逐源流/声道 Source Patching](#f2-1) | P1 | F1.1、F0.4 | — | 已完成（常用配置范围：模型/命令/序列化/三路渲染消费/插入四门/UI 入口/入队预检/R9 跨序列粘贴修复；完整 patch matrix 与轨头引脚登记后续片，[ledger](validation/feature_F/F2.1/39abcfa01/source_patching_ledger_2026-10-23.md)） |
| ☑ | [F2.2 高级三/四点、backtime 与 fit-to-fill](#f2-2) | P1 | F2.1、F0.4 | F3.2（见正文） | 已完成（判定层纯函数+接线+冲突三选+锁轨状态栏反馈；fit 变调填充，实时保调登记 F3.2 条件依赖，[ledger](validation/feature_F/F2.2/aec4f2c15/three_four_point_ledger_2026-10-23.md)） |
| ☑ | [F2.3 真双画面 Trim Viewer 与 JKL 动态修剪](#f2-3) | P1 | F1.1、F0.4；A/B 接口 | — | 已完成（双画面真实保留边界帧=per-asset latest-wins 解码+generation 防旧帧；步进形态 JKL+Enter 单命令提交/Esc 零写入；scrub 收尾信号补缺；连续速度 JKL 与预/后卷登记后续片，[ledger](validation/feature_F/F2.3/21767814a/trim_viewer_ledger_2026-10-24.md)） |
| ☑ | [F2.4 普通 Transform/Opacity 动画与曲线入口](#f2-4) | P1 | F1.1、F0.4 | — | 已完成（核验优先：八验收点七项已有覆盖只记录不重复开发；修复变速关键帧不迁移缺陷=apply/undo 对称比例重映射；导航与曲线 UI 登记后续片，[ledger](validation/feature_F/F2.4/2b8036309/transform_animation_ledger_2026-10-23.md)） |
| ☑ | [F2.5 同钟实时多机位与音频切换策略](#f2-5) | P1 | F1.1、F2.1、F0.1 | — | 已完成（音频策略三模式+实时四路网格真解码+插入侧成对落轨；九路评估已记录登记后续片，[ledger](validation/feature_F/F2.5/00db7c172/multicam_grid_ledger_2026-10-24.md)） |
| ☑ | [F2.6 多机位同步方式与长录制漂移](#f2-6) | P1 | F0.4、F1.1 | F2.5、F3.3（见正文） | 已完成（防错门槛四连[低置信/反相负谷/周期歧义/出界]+TC 替代同步入口+首中末多段采样与速率漂移诊断拒绝+分析中途取消；漂移矫正登记 F3.3 条件依赖，[ledger](validation/feature_F/F2.6/6fad632a2/multicam_sync_ledger_2026-10-24.md)） |
| ☑ | [F2.7 批量素材属性与解释媒体](#f2-7) | P2 | F0.4、F1.1、F2.1 | — | 已完成（解释覆盖核心链：fps 覆盖层/原始探测不改写/既有片段 DaVinci 语义重锚/代理失效/批量入口单事务+逐项报告；面板多素材混合值与 cache 指纹登记后续片，[ledger](validation/feature_F/F2.7/c2b568a88/interpretation_ledger_2026-10-24.md)） |
| ☑ | [F2.8 时间线视频变速与插值质量](#f2-8) | P1 | F0.4、F1.1 | F7.2、F3.2（见正文） | 已完成（核验+两缺陷修复：GPU预览frame_blend回退CPU/optical_flow显式告警不静默；25→50fps与40%慢放验收直测；注册表入册edit.video.retime_interpolation；受控光流登记F7.2，[ledger](validation/feature_F/F2.8/7045b41d6/retime_quality_ledger_2026-10-24.md)） |
| ☑ | [F2.9 手工标题、动态图形与模板生命周期](#f2-9) | P1 | F0.4、F1.1、F2.4 | F9.2（见正文） | 已完成（核心缺口：图形clip刀片/trim守卫防孤儿半段与失步+保存为模板入口/重命名/删除/切页刷新全生命周期接线；滚动roll动画/cue级动画/restore UI/9:16预设/替代字体选择六项登记后续片，[ledger](validation/feature_F/F2.9/193b4bc00/title_lifecycle_ledger_2026-10-24.md)） |
| ☑ | [F2.10 摄影机视频与独立录音的源级同步](#f2-10) | P1 | F0.4、F1.1、F2.1、F3.1 | F2.6、F3.3（见正文） | 已完成（源级绑定核心链：四路消费统一口径/波形-TC-手动三法/F2.6 拒绝门槛防误绑/跨采样率显式归一并修既有 44.1k 缺陷/非破坏单事务；双源混音与 worker 化登记后续片，[ledger](validation/feature_F/F2.10/ba6da523f/audio_sync_ledger_2026-10-24.md)） |
| ☑ | [F3.1 统一音频图与嵌套语义复核](#f3-1) | P0 | F1.1、F0.4 | — | 已完成（三路径闭环复核保持；crossfade 诊断/入队 BLOCKING 预检/预览信号落地；父 retime 重采样与 mixed-fps 登记 AV 一致设计，[ledger](validation/feature_F/F3.1/203e239ec/nested_semantics_ledger_2026-10-23.md)） |
| ☑ | [F3.2 流式实时保调变速](#f3-2) | P1 | F3.1 | — | 已完成（流式 WSOLA 与离线同参数，预听/导出一致；两处语义偏离与后续片登记台账，[ledger](validation/feature_F/F3.2/streaming_preserve_pitch_ledger_2026-10-24.md)） |
| ☑ | [F3.3 采样级音频编辑与迁移](#f3-3) | P1 | F0.4、F3.1 | — | 已完成（有理数时钟+子帧位置/源侧修剪/采样域淡化三字段+命令+预览/成片/Stem 三路同语义消费+整数帧假设盘点；子帧 crossfade 等登记后续片，[ledger](validation/feature_F/F3.3/sample_time_ledger_2026-10-24.md)） |
| ☑ | [F3.4 路由、自动化、响度、Stem 与录音](#f3-4) | P1 | F3.1、F1.1 | F3.3、F3.2（见正文） | 已完成（六缺陷修复[BS.1770-4公式/stem sends/BUS干声/成片静音/stem采样键/设备id类型]+路由sidecar+核验声明；GROUP bus 语义统一等登记后续片，[ledger](validation/feature_F/F3.4/routing_loudness_stems_ledger_2026-10-24.md)） |
| ☑ | [F4.1 场景分组与组前/片段/组后处理](#f4-1) | P1 | F0.4、F1.1 | — | 已完成（组实体+受限命令+微调栈+解析单点+四路消费[预览CPU/GPU/导出CPU/Metal]+指纹防绿缓存；Color页组UI与链段高级操作登记后续片，[ledger](validation/feature_F/F4.1/ef0993564/color_group_ledger_2026-10-24.md)） |
| ☑ | [F4.2 命名调色版本与共享 look 锁定](#f4-2) | P1 | F4.1、F0.4 | — | 已完成（命名版本激活=内容互换/共享默认锁+唯一写路径强制/影响范围预览/插件清单随版本；顺手修复调色编辑抹掉F2.x覆盖表与IO转换器Rational崩溃两处pre-existing缺陷；Color页UI与工程级版本登记后续片，[ledger](validation/feature_F/F4.2/color_versions_ledger_2026-10-24.md)） |
| ☑ | [F4.3 Lightbox、Gallery 与 HDR 参考 still](#f4-3) | P1 | F0.4、F1.1 | F4.2（见正文） | 已完成（基础 Gallery/HDR still 独立验收：float32 EXR sidecar 保留>1.0/负值/渐变+schema2 精度/工作空间/显示变换声明+旧JPEG标明限制+sidecar一致行为+difference 对比模式；抓帧端 float 读回与 Lightbox 对话框登记后续片，[ledger](validation/feature_F/F4.3/hdr_still_ledger_2026-10-24.md)） |
| ☑ | [F4.4 批量镜头匹配与人工修正](#f4-4) | P2 | F0.4、F5.1 | F4.1、F4.2、F4.3（见正文） | 已完成（契约盘点=零算法零消费者→补齐可解释曝光/WB/分布匹配[迭代灰世界+肤色/中性锚]+四排除守卫+GradingMath 数值映射+批量提交[F4.1微调栈+F4.2版本锚+过期拒绝]；已知偏移恢复±0.08stop；UI 预览面板登记后续片，[ledger](validation/feature_F/F4.4/shot_match_ledger_2026-10-24.md)） |
| ☑ | [F4.5 二级调色参数与遮罩动画覆盖](#f4-5) | P1 | F0.4、F1.1、F5.1 | F7.1、F4.2（见正文） | 已完成（选定 Qualifier 亮度区间五环贯通[记录/求值/曲线/逐参数复制/静态切换]+幂等求值枢纽接入+◆白名单唯一判据；seek/反放/变速/Undo/序列化一致值 11 测试；sat/hue 扩展与点集拓扑登记后续片，[ledger](validation/feature_F/F4.5/qualifier_keyframes_ledger_2026-10-24.md)） |
| ☑ | [F4.6 HDR 分区创作工具与高光塑形](#f4-6) | P2 | F5.1、F0.4 | F4.5、F5.2（见正文） | 已完成（核验收口：既有 apply_soft_clip=scene-linear Toe/Shoulder 两区+区内曝光即最少几区实现，CPU/GPU/MSL 权威文本逐条等价[AUD-0911/0912]；新增验收向量 6 项[肤色保留/边界连续/负值黑位/超白不翻转/线性曝光]；分区轮 UI 无实际需求按计划记录条件延期不宣称，[ledger](validation/feature_F/F4.6/hdr_zones_ledger_2026-10-24.md)） |
| ☑ | [F5.1 输入—工作—显示—输出色彩闭环](#f5-1) | P0 | F0.4、F1.1 | F4.1（见正文） | 已完成（三路语义收口声明+CPU OETF 单点化+GLSL 常数合同测试；HDR 预览统一登记 F5.2 候选，[ledger](validation/feature_F/F5.1/43e00f850/color_pipeline_ledger_2026-10-23.md)） |
| ☐ | [F5.2 校准监看与实际输出质量](#f5-2) | P1 | F5.1、F0.1 | — | 未开始 |
| ☑ | [F5.3 相机 RAW 与媒体支持矩阵](#f5-3) | P1 | F5.1、F0.4 | — | 已完成（10 家族六维能力矩阵+真实原片验收[10 文件逐个打开解码一帧,扩展名识别≠完成]+照片RAW/ToposRAW单列+BRAW/R3D 真实原片在库无SDK→诊断+转码建议不伪装；SDK 引入与 CinemaDNG 序列登记后续片，[ledger](validation/feature_F/F5.3/media_matrix_ledger_2026-10-24.md)） |
| ☑ | [F5.4 常见输入解释与通用图像序列导入](#f5-4) | P1 | F0.4、F1.1、F5.1 | F2.7、F6.6、F6.4（见正文） | 已完成（通用序列模型[单资产/缺帧诊断/指纹/不合流]+VFR PTS 生产绑定全链[抽取→探测metadata→插入→mapper]音画半帧对齐+field_order/SAR/rotation 组合核验与 passthrough 守卫拒绝；序列插入链与端到端几何对拍登记后续片，[ledger](validation/feature_F/F5.4/input_interpretation_ledger_2026-10-24.md)） |
| ☑ | [F6.1 FCPXML/OTIO 支持子集与外部往返](#f6-1) | P1 | F0.4、F1.1 | F3.3、F4.2（见正文） | 已完成（核验后补强：Resolve v1.8 方言黄金工程逐切点/源帧身份核验+真实方言源入点被清零缺陷修复[start>tcStart>asset 三级优先]；OTIO 无依赖诚实降级；58+5 测试；实机样本与子帧/版本映射登记后续片，[ledger](validation/feature_F/F6.1/interchange_roundtrip_ledger_2026-10-24.md)） |
| ☑ | [F6.2 回原片 reconform、代理重连与归档](#f6-2) | P1 | F0.4、F1.1 | F6.1（见正文） | 已完成（核验+缺口修复：安全重连匹配器[名/大小/短哈希三级,同名异内容强制人工,仅同名绝不自动]+代理/原片重套验证[分辨率/时间基诊断]+切点恒定断言；归档 9 既有测试；LUT 收集与对话框接线登记后续片，[ledger](validation/feature_F/F6.2/relink_archive_ledger_2026-10-24.md)） |
| ☐ | [F6.3 AAF 声音后期交接](#f6-3) | P1 | F6.1、F3.1、F3.4 | F3.3（见正文） | 条件延期（aaf2 依赖门未开+真实接收软件验收不可执行——计划原文'接收端未验收保持未完成'；已交付可执行交接建议[Stems+routing.json+CSV 明确不冒充/FCPXML 外转路径]与不冒充锁定测试，[ledger](validation/feature_F/F6.3/aaf_gate_ledger_2026-10-24.md)） |
| ☑ | [F6.4 PNG/TIFF 等通用图像序列输出](#f6-4) | P2 | F5.1、F1.1 | — | 已完成（核验+恢复语义修复：既有 PNG/TIFF/EXR/DPX 导出器缺'已验证帧 manifest'——跳过只看文件名存在；新增 VerifiedFrameManifest[大小+头部哈希指纹,损坏/截断/异配置残留重导]接入 exporter；8 新测试+57 既有绿；DELIVER 恢复按钮与实机回导登记后续片，[ledger](validation/feature_F/F6.4/frame_manifest_ledger_2026-10-24.md)） |
| ☑ | [F6.5 素材入库、组织与受控校验拷贝](#f6-5) | P1 | F0.4、F1.1、F1.2 | F2.10、F5.4（见正文） | 已完成（核验组织面既有+交付受控校验拷贝核：整文件SHA-256源/目标校验[快速指纹不冒充]/同名异卡冲突不覆盖良好拷贝/不完整源与目的盘断开取消逐项可操作报告/源卡只读/摄影卡目录结构保留；8测试；UI入口与多目的备份登记后续片，[ledger](validation/feature_F/F6.5/verified_ingest_ledger_2026-10-24.md)） |
| ☑ | [F6.6 项目代理与渲染缓存管理流程](#f6-6) | P1 | F0.1、F1.1、F1.2 | F5.4、F6.2、F7.3（见正文） | 已完成（核验56代理测试文件既有+补安全清理缺口：purge_proxies[只删manifest登记项/路径逃逸拒绝/GENERATING活动保护/孤儿模式源在不动/原片只读]+proxy_usage_report占用统计；5新测试+91既有绿；UI清理入口与目录迁移面板登记后续片，[ledger](validation/feature_F/F6.6/proxy_cache_ledger_2026-10-24.md)） |
| ☑ | [F7.1 跟踪、Roto、抠像与修补质量](#f7-1) | P1 | F0.3、F1.1、F5.1 | — | 已完成（核验为主：跟踪链[点/平面/失败检测/置信度/手动修正/分段重跟/缓存失效]既有覆盖超计划假设+补镜头集缺口[scene_cut 切换不污染后续帧+独立运动模糊镜头置信必须准确]；真实修补=ai_remove 唯一/人脸无生产接线/发丝镜头如实声明；190+5 测试，[ledger](validation/feature_F/F7.1/tracking_quality_ledger_2026-10-24.md)） |
| ☑ | [F7.2 去噪、补帧与运动相关时域一致性](#f7-2) | P1 | F7.1、F0.4 | — | 已完成（核验为主：时域去噪状态机重置规则[换素材/seek跳变/参数/分辨率/反向播放/切镜]10项既有测试全锁定+补帧时域一致性5项[重复求值逐位可复现/访问顺序无关=随机seek一致/端点透传无鬼影/纯函数对称]；降噪链无随机源确定性；RIFE 缺模型显式降级不偷降交付；54+16 测试，[ledger](validation/feature_F/F7.2/denoise_interpolate_ledger_2026-10-24.md)） |
| ☑ | [F7.3 复杂节点图、缓存与 Render-in-Place](#f7-3) | P2 | F7.1、F7.2、F1.2 | — | 已完成（核验+缺口修复：缓存键[类型/参数/禁用/render_mode/预览导出隔离]与 dirty cone 精确域既有158测试+烘焙源签名[上游锥参数指纹 SHA256,写入bake节点随序列化往返]修复'更新原图悄悄播放旧烘焙'[上游变化→stale,锥外不误报,旧烘焙None不冒称]；15+158 测试；过期徽标 UI 与真实工程性能基准登记后续片，[ledger](validation/feature_F/F7.3/rip_cache_ledger_2026-10-24.md)） |
| ☑ | [F8.1 命名工程快照与版本比较](#f8-1) | P2 | F1.2、F0.4 | F4.2（见正文） | 已完成（工程级命名快照模型：内容寻址 SHA256+source_revision+依赖清单[素材/LUT]+secret 拒绝+恢复哈希校验[篡改拒绝]依赖缺失报告不阻断+结构化只读比较[移动/修剪/换源/效果/调色/音频/增删]不污染 Undo+自动备份/命名版本/调色版本三层区分；14 测试；UI 面板与 IO 接线登记后续片，[ledger](validation/feature_F/F8.1/project_snapshots_ledger_2026-10-24.md)） |
| ☑ | [F8.2 带版本锚点的审片评论与 marker](#f8-2) | P2 | F8.1、F0.4 | — | 已完成（ReviewComment 模型[稳定id/作者/状态/版本哈希/三重锚:时间线帧+素材源帧+clip]+JSON 往返 Unicode 安全重复导入幂等+隐私清理剥离作者+评论→marker 状态配色+重定位置信度分级[exact 1.0 源锚跨fps不变/timeline_only 0.5 无锚近似/stale 0.0 删镜头或源帧出窗标失效——有源锚绝不静默挪到别的镜头]；12 测试；审片面板 UI 登记；不默认部署云服务，[ledger](validation/feature_F/F8.2/review_comments_ledger_2026-10-24.md)） |
| ☑ | [F8.3 多 take 候选/Audition](#f8-3) | P2 | F0.4、F1.1 | F8.1、F2.2（见正文） | 已完成（核验通用多take此前不存在→片段级候选容器模型：非破坏切换[ripple 邻片后移量可预览/fit 裁现时长]/缺源 offline 不可激活活动自动回退容器不丢/flatten 明确拆平其余候选保留/混合fps时长换算；与F4.2调色版本、ADR素材级三重区分；9 测试；UI 面板与工程字段接线登记后续片，[ledger](validation/feature_F/F8.3/audition_ledger_2026-10-24.md)） |
| ☑ | [F9.1 文稿驱动粗剪与删除口头语](#f9-1) | P2 | F0.4、F3.1、F1.1 | F2.1、F3.3（见正文） | 已完成（核验地基[词级时间戳双路径/compile_plan 白名单/state_fingerprint 过时拒绝/wf-talking-head-fine-cut 审批链]均有测试+补缺口：detect_filler_words[zh/en 词表帧域区间,低置信<0.6 不删→review 人工排除,标点大小写归一,混合语言并集]+timings_fingerprint 幂等锚[重试不重复删]+build_filler_removal_plan[PREVIEW+白名单 op 走既有审批链不新增旁路]；首版帧对齐[F3.3 条件依赖计划允许]；9 测试，[ledger](validation/feature_F/F9.1/filler_removal_ledger_2026-10-24.md)） |
| ☑ | [F9.2 多语言字幕、AI 质量门与可恢复批处理](#f9-2) | P2 | F0.4、F1.1、F1.3 | F9.1（见正文） | 已完成（核验地基厚实：六格式[srt/vtt/ass/ttml/dfxp/scc]时间基往返+inspect QC[行长/速率/重叠/越界 CJK混算]+交付级六指标 blocking 硬门+AI 质量门三件套[硬门槛/KPI/乐观锁]+翻译链+竖屏 safe-area 均有测试→补缺口：翻译后 QC[翻译产物此前无再检查,目标语言行长/速率差异巨大——qc_translated_track 自动过 inspect,error 判定 fail,warning 保留人工]；4 新测试；画布变换重排登记后续片，[ledger](validation/feature_F/F9.2/subtitle_quality_ledger_2026-10-24.md)） |
| ☑ | [F9.3 多版本交付、QC 与任务恢复](#f9-3) | P1 | F1.1、F1.2、F5.1 | F3.4、F6.4、F8.1、F9.2（见正文） | 已完成（核验队列恢复/QC/probe-manifest 地基[65 测试]→补两缺口：多版本编译器[LinkedVersion×N→ExportJob config 复用既有队列,画幅分辨率映射,命名冲突与素材依赖先检查不产半套,source_revision+snapshot_hash 冻结源版本不用旧缓存续新版本]+PCM 音频帧计数[修复 probe 跳过 AudioFrame 缺口]；9 新测试；UI 入口与 manifest 接线登记后续片，[ledger](validation/feature_F/F9.3/multiversion_delivery_ledger_2026-10-24.md)） |
| ☐ | [F10.1 多人协作、锁定与冲突](#f10-1) | P3 | F8.1、F8.2、F1.2 | 显式立项条件 | 未开始 |
| ☐ | [F10.2 VST3/AU 音频插件宿主](#f10-2) | P3 | F3.2、F3.3、F3.4、F1.2 | 显式立项条件 | 未开始 |
| ☐ | [F10.3 广播/影院与高级 HDR 交付](#f10-3) | P3 | F5.1、F9.3 | F5.2（见正文）；显式立项条件 | 未开始 |
| ☐ | [F10.4 大型 3D/高级合成扩展](#f10-4) | P3 | F7.1、F7.2、F7.3 | 显式立项条件 | 未开始 |
| ☐ | [F10.5 第三方视频效果插件宿主（如 OpenFX）](#f10-5) | P3 | F1.2、F1.3、F5.1、F7.3 | F4.5（见正文）；显式立项条件 | 未开始 |
| ☐ | [F10.6 远程渲染与可移植交付任务](#f10-6) | P3 | F1.2、F6.2、F9.3 | F10.1（见正文）；显式立项条件 | 未开始 |

## 2. 源码入口目录（按符号核验，不冻结旧行号）

下列别名指向**已有目录**；任务中只列相关文件/符号，不授权修改整个目录。实施者委派时须展开成绝对路径并给出文件白名单。

| 别名 | 绝对路径 | 主要已有入口 |
|---|---|---|
| EDIT | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/` | `timeline_v2.py`、`timeline_ops.py`、`commands.py`、`timeline_project_transaction.py`、`timeline_frame_mapper.py` |
| EUI | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/` | `source_viewer.py`、`edit_page.py`、`timeline_panel.py`、`edit_properties_panel.py`、`trim_numeric_dialog.py`、`multicam_panel.py` |
| HOST | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/` | `edit_controller.py`、`page_controller.py`、`layer_controller.py`、`file_controller.py` |
| COLOR | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/` | `core/color_session.py`、`grade_clipboard.py`、`grade_keyframes.py`、`preset_meta.py`、`ui/presets_panel.py`、`ai/color_context.py` |
| PROJECT | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/project/` | `topos_project.py`、`color_stack.py`、`topos_project_io.py`、`topos_project_controller.py`、`save_router.py`、`project_manager.py` |
| DELIVER | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/deliver/` | `core/video_export_preflight.py`、`video_job_runner.py`、`video_batch_export_controller.py`、`export_queue.py`、`ui/deliver_workspace.py` |
| ADAPTER | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/adapters/` | `fcpxml_adapter.py`、`otio_adapter.py`、`interchange_types.py` |
| MEDIA | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/media/` | `format_registry.py`、`factory.py`、`ffmpeg_source.py`、平台硬件目录、metadata/色彩字段 |
| CM | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/color/` | `color_transform_provider.py`、`color_space_descriptor.py`、`types.py` |
| COMP | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/composite/` | `core/node_engine.py`、`evaluators/`、`render/`、`nodes/tracker_node.py`、`frame_interpolate_node.py`、`ml_roto.py` |
| AI | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ai_editing/` | `analysis_service.py`、`snapshot_service.py`、`schema.py`、`workflow.py`、`edit_api.py`、`execution_policy.py`、`quality_gate.py` |
| EFFECT | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/shared/effects/` | Catalog/Registry、能力/参数 schema 与 preview/export 策略 |
| VIDEO | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/video/` | `monitor_output.py`、`playback_frame_trace.py`、帧供应/上传设施 |
| TEST | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/` | 先复用 `edit/`、`color/`、`deliver/`、`project/`、`composite/`、`shared/color/`、`video_denoise/` 中对应测试 |

## Phase F0 · 承接性能工程，冻结功能验收契约

<a id="f0-1"></a>

### F0.1 A/B 收口与开工基线

- [x] 状态：已完成（A 收口 `2d7f18e0f`〔2026-10-22，旧 manifest §1〕；B 收口 `0b57c322d`〔2026-10-24，机器 B 交付七类产物，经合并 `4c22cacf5` 入 fix-ui，macOS 交叉复查 + L1 测试可跑性修复 `d2ce07f2e`〕；终版 manifest 按旧 §2.3 规则替换阻塞映射——F1.3 Windows 行/F5.1 D3D11 行/F2.5/F6.6 重验四子项重新处置，F5.2/F6.3/F10.x 维持延期）；优先级：P0；性质：核验。证据：[F0.1 终版 manifest](validation/feature_F/F0.1/0b57c322d/manifest_2026-10-24.md)（旧开工期记录存档于 [2d7f18e0f](validation/feature_F/F0.1/2d7f18e0f/manifest_2026-10-22.md)）。
- 依赖：§0.1 的 A/B 接续记录。
- 入口：A/B 的报告、遥测、实际 backend/能力探测和构建产物，不修改业务。
- 实施：采集新 HEAD/dirty 与依赖指纹；复跑 A/B 最小代表场景，登记可用/回退/延期项；列出延期对本表的阻塞关系。核验 CLI/ZCode 的小任务结果回收，不把代理配置问题混进产品能力。
- 验收/交付：A/B 接续 manifest、同机基线与设备覆盖表；关键取消/关闭/渲染链稳定。未具备相应能力的 F 任务保持未验收，不靠更改声明消除阻塞。

<a id="f0-2"></a>

### F0.2 当前能力盘点与差量清单

- [x] 状态：已完成（5 路并行审计，全部 file:line 证据；四桶分类 + 注册表矛盾 R1–R7）；优先级：P0；性质：核验。证据：[capability ledger](validation/feature_F/F0.2/bde78291b/capability_ledger_2026-10-22.md)。
- 依赖：F0.1。
- 入口：EDIT `capability_registry.py`、相关 feature module/UI→HOST→service→预览/导出路径；本表 §4、§5 与 §8 的覆盖/证据索引。
- 实施：每项检查生产入口、真实消费者、Undo/持久化、测试与当前设备；将“源码确认缺口 / 仅建议补强 / 已有能力覆盖 / 需复测”分别记录。组调色、版本、协作、AI 等不能只凭检索未命中判定不存在。
- 验收/交付：逐项 evidence ledger。已有实现通过对应场景后可关闭为“已有能力覆盖”；部分实现只记录剩余差量，其余任务保留未开始。

<a id="f0-3"></a>

### F0.3 黄金工程、媒体与验证驱动

- [x] 状态：已完成（媒体生成器/最小+扩展工程 builder/结构快照器/T 级 12 用例全绿/G 级真实窗口 13 项全过；新发现 R8 死信号、R9 跨序列粘贴跳过已并入 F0.2 ledger §5）；优先级：P0；性质：基建。证据：[F0.3 manifest](validation/feature_F/F0.3/c2c0a841c/manifest_2026-10-22.md)。
- 依赖：F0.2。
- 入口：TEST 既有真实宿主测试/素材、EDIT render plan、DELIVER 输出探针。
- 实施：建立最小工程与扩展工程，覆盖双序列/嵌套、链接 A/V、混合帧率、效果关键帧、字幕、调色和离线媒体；加入精确脉冲/正弦/反相 stereo、色彩/alpha 梯度和真实镜头。驱动复用生产 handler/service；G 级验证须打开真实窗口，不以 offscreen 或 mock 冒充。
- 既有能力专项：Smart Bin 在关键词/评级修改、导入/移除后正确更新；Adjustment Clip 在重叠、轨道禁用、嵌套边界与效果顺序下只作用于声明范围；多时间线/堆叠视图切换与跨时间线复制保持身份、时间基、链接素材和 Undo。先核已支持范围，不把这些已有功能登记为从零新增；缺生产闭环时再列具体差量。
- 验收/交付：输入与预期 manifest、分组运行清单、结构化报告。源文件只读，测试只动隔离配置/工程；无 GPU、设备或外部软件时明确未验证。新 harness 参数实现后再提供可执行命令，不预写不存在的 CLI。

<a id="f0-4"></a>

### F0.4 时间、色彩、身份与迁移契约

- [x] 状态：已完成（时间基/区间开闭/舍入/漂移保证/身份表/迁移契约 ADR 冻结；独立 oracle 43 向量全绿；现实现无需改动，未提前写空字段）；优先级：P0；性质：设计后最小实现。证据：[ADR](validation/feature_F/F0.4/ef91445f2/adr_timebase_identity_migration_2026-10-22.md)。
- 依赖：F0.2。
- 入口：EDIT `timeline_v2.py`、`timeline_frame_mapper.py`；PROJECT 模型/迁移；CM；A/B trace/租约协议。
- 实施：明确源帧、时间线帧、采样点、有理数时间基及区间开闭；统一 clip/asset/sequence/grade/revision 身份和跨页快照。为采样编辑、调色组/版本、评论、候选 take 的新增 schema 各列独立迁移，而非一次大改所有模型。
- 验收/交付：兼容性 ADR 和精确向量；24/25/29.97/59.94 与 48kHz 映射不累计漂移；旧文件/未知字段/其他模块 payload 保留。后续任务按需落地迁移，不提前写空字段宣称能力完成。

## Phase F1 · 先建立可靠发布门禁

<a id="f1-1"></a>

### F1.1 历史缺陷复测与跨页闭环

- [x] 状态：已完成（20 项当前通过含补测试/真实窗口证据；D03 修复；Q01 部分修复——原 __del__ 场景已修，并发 close 楔死根因定位为 libav↔GIL 锁序反转、根治移交 F1.2）；优先级：P0；性质：回归，复现后才修。证据：[replay ledger](validation/feature_F/F1.1/d2833a069/replay_ledger_2026-10-22.md)。
- 依赖：F0.3、F0.4。
- 入口：EDIT commands/audio/captions；HOST；DELIVER workspace/settings/context/runner；附录 H 的历史 26 项。
- 实施：逐项重放旧复现参数；核验自定义范围、非当前序列、嵌套、播放头、变速/Stem、字幕、锁轨与 Undo。当前已有 duration_frames/sequence-map 修复接线，不能直接回退重写。若仍复现，先补 focused test，再修原生产入口；严重正确性问题在本包先修，后续功能不能建立在已知错误之上。
- 验收/交付：26 项逐项给出“当前通过 / 仍复现并已修 / 环境待验证”和证据；关键内容/状态损失项不得带入下一阶段。Edit→Color→Composite→Deliver→保存/重启后的帧数、身份、时码、效果、字幕和音频一致。

<a id="f1-2"></a>

### F1.2 工程与任务恢复的故障路径

- [x] 状态：已完成（保存中断/半写/损坏候选/备份恢复注入全过；Q01 四层根治：GC 停靠/常驻 closer 空闲窗口/每实例 RLock/worker 泄清夹具；金属渲染器全量累积楔点判定为环境性基建议题、R22 子进程门禁收容，单文件恒绿）；优先级：P0；性质：补强。证据：[fault injection ledger](validation/feature_F/F1.2/4811a68fe/fault_injection_ledger_2026-10-23.md)。
- 依赖：F0.3、F0.4。
- 入口：PROJECT io/controller/save_router；EDIT `project_archive.py`；DELIVER `export_queue.py`、runner；MEDIA 生命周期。
- 实施：隔离子进程/临时目录中注入保存中断、写入失败、缺媒体、任务取消和重启；核验原子保存、备份、队列恢复及重复运行。不得杀用户正在工作的实例，也不在用户盘上真实制造磁盘满。
- 验收/交付：损坏候选不覆盖最后可用工程；旧版和未知 payload 可恢复；任务不重复提交成片/覆盖既有输出；临时文件、decoder、GPU 租约和线程在失败/关闭后可回收。给出恢复入口和报告，不仅日志。

<a id="f1-3"></a>

### F1.3 统一能力声明、提示与测试分层

- [x] 状态：已完成（R1 漂移语义/R3 头注/R4 Cue Sheet DORMANT/R5+R6 注册表扩至 16 条/R8 死信号移除/R7 两层设计登记/R9 归 F2.1；零漂移断言入册；顺手修 export_manifest present 基线缺陷）；优先级：P1；性质：补强。证据：[consistency ledger](validation/feature_F/F1.3/0023acb4c/capability_consistency_ledger_2026-10-23.md)。
- 依赖：F0.2、F1.1。
- 入口：EDIT capability_registry、EFFECT、DELIVER preflight、各 UI/i18n。
- 实施：清理旧表/休眠类/新 registry 对 FCPXML、保调、效果导出等的矛盾；UI、AI、preflight 共用真值。区分可用/部分/不可用/未探测；“即将推出”与实际入口一致。每项标产品变体、OS、依赖和证据等级。
- 验收/交付：能力矩阵与用户提示一致；不支持的操作有原因与替代路径，不静默丢效果/音频。测试按逻辑、真实媒体、GPU、外设和外部软件分组，有超时/退出码/skip 理由；测试夹具修复不能代替宿主回归。

<a id="f1-4"></a>

### F1.4 快捷键、工作区与硬件控制的效率验收

- [x] 状态：已完成（框架层验收齐备复用；修复 Edit JKL/Shift+B 文本框屏蔽实际缺陷；Tangent 一手势一 Undo 边界+切层 rebase+副屏回落补测；DPI 处理/Tangent 真机/运行时拔屏登记为已知缺口）；优先级：P2；性质：既有能力验收/补强。证据：[efficiency ledger](validation/feature_F/F1.4/a50417ec2/efficiency_acceptance_ledger_2026-10-23.md)。
- 依赖：F1.1、F1.3。
- 条件依赖：F4.5：新增调色动画/参数映射；真实控制台专项需要对应硬件，不能阻塞键盘/工作区验收。
- 入口：已有 `src/shared/ui/shortcut_manager.py`、`shortcut_scope.py`、`src/shared/layout/`；HOST color_workspace_controller；COLOR integrations/tangent_bridge；各新增动作注册点。均先复用，不新建快捷键框架。
- 实施：把本计划新增动作注册到既有 registry，核自定义按键冲突/导入导出、文字输入与浮窗作用域、跨产品工作区保存、双屏布局/DPI 恢复和焦点；为 Tangent 等已声明支持的控制器核当前镜头/参数反馈、断线重连与 Undo 合并。
- 验收/交付：同一按键在 Edit/Color/Composite、标题文本框、Motion Graphic 浮窗只作用于正确目标；拔掉副屏后窗口可找回。控制台连续调整按契约合并 Undo，切镜/切页不改错 grade；无设备则 H 子项未验证，不写成产品尚无控制台支持。

## Phase F2 · 剪辑效率与多机位

<a id="f2-1"></a>

### F2.1 逐源流/声道 Source Patching

- [x] 状态：已完成（常用配置范围，2026-10-23；六片提交 1329aaa3f/305455a63/d8a814f72/ed4e8e667/39abcfa01；完整逐声道→逐目标轨 matrix、轨头引脚 UI 登记为后续片，见 ledger §5）；优先级：P1；性质：差量建设。证据：[source patching ledger](validation/feature_F/F2.1/39abcfa01/source_patching_ledger_2026-10-23.md)。
- 依赖：F1.1、F0.4。
- 入口：EUI source_viewer/edit_page/timeline_panel；HOST edit_controller；EDIT commands/模型、media metadata。
- 实施：先盘点现有 V/A 目标轨；增加源视频/音频流、源声道到目标轨的显式映射及混合值提示，先支持选定单声道/立体声常用配置。插入/覆盖使用一次领域事务，禁写锁轨，映射随用户选择而非靠 UI 临时变量保存。
- 验收/交付：多声道素材可只把指定声道送 A1/A3；video-only/audio-only 不带入其他流；声道布局、时长、单次 Undo/Redo、保存/导出一致，缺流/冲突有明确反馈。

<a id="f2-2"></a>

### F2.2 高级三/四点、backtime 与 fit-to-fill

- [x] 状态：已完成（2026-10-23，提交 aec4f2c15；fit 音频为变调填充，实时保调按计划原文登记 F3.2 条件依赖，Alt+X 清 range 键盘缺口登记后续）；优先级：P1；性质：差量建设。证据：[three/four-point ledger](validation/feature_F/F2.2/aec4f2c15/three_four_point_ledger_2026-10-23.md)。
- 依赖：F2.1、F0.4。
- 条件依赖：F3.2：实时保调填充；不要求普通 backtime/定长编辑等待音频保调。
- 入口：EUI source_viewer/edit_page；EDIT timeline_ops/frame mapper/commands。
- 实施：复用现有 In/Out 和 Insert/Overwrite；补目的 In/Out、由目的 Out 倒推和四点冲突规则；冲突明确选修源范围/修目的范围/变速填充/取消。变速方案调用已有时间映射，不直接改 clip 长度掩盖源映射。
- 验收/交付：指定片段恰好结束在目标切点；不同帧率、非零入点、handles 不足、锁轨、链接音频、Undo 和导出均满足 F0.4 区间契约。fit-to-fill 涉及实时保调时依赖 F3.2，否则声明实际音频策略。

<a id="f2-3"></a>

### F2.3 真双画面 Trim Viewer 与 JKL 动态修剪

- [x] 状态：已完成（步进形态，2026-10-24，提交 21767814a；连续速度 JKL 与预/后卷循环登记后续片，见 ledger §5）；优先级：P1；性质：升级。证据：[trim viewer ledger](validation/feature_F/F2.3/21767814a/trim_viewer_ledger_2026-10-24.md)。
- 依赖：F1.1、F0.4；继承 A/B 的呈现/租约能力。
- 入口：EUI `trim_numeric_dialog.py` 的 TrimMonitorDialog、timeline_panel；EDIT 帧请求/trim commands。
- 实施：把两侧时间码升级为真实切点帧；复用帧供应与渲染队列，拖动 latest-only、旧请求取消；增加预/后卷循环、JKL、commit/cancel，维持工具窗不抢焦点。预听走现有音频时钟，不新建竞争播放源。
- 验收/交付：双画面对应真实保留边界，单帧修剪与最终输出一致；鼠标/键盘取消不留 revision，提交一次 Undo；快速拖动/切源/关闭不显示旧 generation，不泄漏纹理。

<a id="f2-4"></a>

### F2.4 普通 Transform/Opacity 动画与曲线入口

- [x] 状态：已完成（核验+缺陷修复，2026-10-23，提交 2b8036309；前后关键帧导航与插值/曲线编辑 UI 入口登记后续片，见 ledger §3）；优先级：P1；性质：核验后补入口。证据：[transform animation ledger](validation/feature_F/F2.4/2b8036309/transform_animation_ledger_2026-10-23.md)。
- 依赖：F1.1、F0.4。
- 入口：EUI edit_properties_panel；EDIT keyframe/schema/commands；复用既有共享曲线控件，不另造求值器。
- 实施：先验证负位移、大角度、opacity 值域及 clip/effect 时域；补普通属性 ◆、前后关键帧导航、插值/曲线编辑。程序化同步控件加状态锁；跨轨移动、变速/拆分按 canonical 时间映射迁移动画。
- 验收/交付：位置 -250/500、角度 -45/180 等不被夹成 0–100；锁轨零写入；add/remove/Undo/保存恢复/预览导出一致。已有入口若验收通过则只记录覆盖，不重复开发。

<a id="f2-5"></a>

### F2.5 同钟实时多机位与音频切换策略

- [x] 状态：已完成（片1 音频策略 `20864f8a4` + 片2 实时四路网格 `00db7c172`，2026-10-24；九路扩展按四路证据评估后登记后续片，见 ledger §4/§5）；优先级：P1；性质：升级。证据：[multicam grid ledger](validation/feature_F/F2.5/00db7c172/multicam_grid_ledger_2026-10-24.md)。
- 依赖：F1.1、F2.1；F0.1 已验证的多路预算与资源协议。
- 入口：EUI `multicam_panel.py`；EDIT `multicam.py`、scheduler/切角命令；HOST edit_controller。
- 实施：先四路实时网格，由一个主时钟驱动，独立有界解码/缩放预算；优先保证 Program 输出，降质/掉监看帧可解释。明确固定主音轨、仅切视频、音频跟随模式；再按四路证据评估九路等扩展。
- 验收/交付：各格显示同一时间线时刻；切角点准确，音频策略可撤销且导出一致；暂停/seek/速率/缺路/最小化恢复正确。记录同机负载与掉帧，不能以缩略图更新冒充实时四路通过。

<a id="f2-6"></a>

### F2.6 多机位同步方式与长录制漂移

- [x] 状态：已完成（同步分析全量，2026-10-24，提交 6fad632a2；速率漂移矫正登记 F3.3 条件依赖、素材级 marker 与 TC fps 持久化登记后续片，见 ledger §6）；优先级：P1；性质：升级。证据：[multicam sync ledger](validation/feature_F/F2.6/6fad632a2/multicam_sync_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F2.5：实时多机位界面联验；F3.3：采样级漂移矫正。同步分析可先做。
- 入口：EDIT `multicam_sync.py`、`multicam.py`；EUI multicam_sync_result_dialog。
- 实施：保留波形相关，增加 timecode/In/marker 同步入口；首/中/末段采样评估漂移和置信度，结果先预览差异再事务提交。区分固定 offset、速率漂移和错误匹配；不默认追溯平移已编辑片段。
- 验收/交付：无开头共同音频可用替代同步；错时码、静音/反相/重复音乐不写错误偏移；长访谈首末均可检查，取消无写入。未支持漂移矫正时诊断并保留原片，不伪称已同步。

<a id="f2-7"></a>

### F2.7 批量素材属性与解释媒体

- [x] 状态：已完成（解释覆盖核心链，2026-10-24，提交 c2b568a88；属性面板多素材混合值、production info 批量、cache 解释指纹、色彩解释登记后续片，见 ledger §5）；优先级：P2；性质：核验后建设。证据：[interpretation ledger](validation/feature_F/F2.7/c2b568a88/interpretation_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F2.1。
- 入口：HOST file/edit controller；EUI 属性入口；MEDIA metadata；PROJECT 资产/重连服务。
- 实施：区分原始探测 metadata 与用户解释覆盖；多选显示混合值，可批量修改标签、帧率解释、声道映射等支持项。重新解释时同步相关 clip/timebase、代理/cache 失效与受影响项目提示，不只改表格。
- 验收/交付：一次事务或逐项失败报告；撤销/重开可恢复；原始媒体不改写，代理/原片切换不串解释，失败不留下半更新。大批任务可取消且 UI 可响应。

<a id="f2-8"></a>

### F2.8 时间线视频变速与插值质量

- [x] 状态：已完成（核验+缺陷修复，2026-10-24，提交 7045b41d6；受控光流 provider 与质量样片登记 F7.2，实时音频反放登记 F3.2，见 ledger §6）；优先级：P1；性质：既有变速验收/差量建设。证据：[retime quality ledger](validation/feature_F/F2.8/7045b41d6/retime_quality_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F7.2：光流/模型时域质量与缓存验证；F3.2：实时音频保调联验。普通视频变速不等待音频扩展。
- 入口：EDIT clip_params 的 SpeedParams、timeline_frame_mapper、速度命令/preview/export；EUI edit_properties_panel；复用 COMP 补帧/光流服务的受支持接口。
- 实施：先核 nearest/frame_blend 与速度点 UI 已有链路，明确 frame position/speed 曲线、冻结/反放、采样时刻和源边界。时间线 optical_flow 当前隐藏，不能因模型枚举或 Composite RIFE 节点存在就声明可用；确需时先打通一个受控 provider，再开放质量/缓存/设备策略。
- 验收/交付：25→50fps、40% 慢放、平滑速度斜坡、非零入点、冻结/反放，经 trim/split/Undo/重开后保留正确源映射；声明支持的插值预览/缓存/成片一致。遮挡/切镜/快速运动有质量样片，不得把 optical_flow 静默当 nearest；不可实时可明示缓存路径。

<a id="f2-9"></a>

### F2.9 手工标题、动态图形与模板生命周期

- [x] 状态：已完成（守卫+生命周期接线，2026-10-24，提交 193b4bc00；滚动片尾 roll 动画、cue 级动画接线、restore/sync UI、9:16 预设与字号自适应、替代字体选择、批量样式六项登记后续片，见 ledger §5）；优先级：P1；性质：已有用户流程专项验收/补强。证据：[title lifecycle ledger](validation/feature_F/F2.9/193b4bc00/title_lifecycle_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F2.4。
- 条件依赖：F9.2：AI/多语言字幕联动；无需模型即可完成手工标题和字幕图形任务。
- 入口：EUI edit_effects_panel 的 _populate_titles；EDIT title_templates、captions/graphics 的 CaptionGraphicBinding；HOST on_caption_effect_apply；既有图形编辑器/模板版本服务。
- 实施：核 Title/Lower Third/Callout/Credits/Social Vertical/用户模板和 Caption Effects 已有生产入口；补模板复制/改名/迁移、入出场动画与时长拉伸、逐条/轨级样式继承、图形同步/脱离/恢复。复用字体解析、安全框和图形事务，不新增平行标题 schema。
- 验收/交付：手工片头、采访条、滚动片尾保存为模板并用于第二条时间线；改文本/时长、trim/split、重开、Undo 后不重复文字、不残留旧样式。16:9→9:16/1:1、Mac/Windows 的字形/断行/安全框与最终像素验收；缺字体准确报告依赖，合法替代需明确选择。

<a id="f2-10"></a>

### F2.10 摄影机视频与独立录音的源级同步

- [x] 状态：已完成（源级绑定核心链，2026-10-24，提交 ba6da523f；双源混音、波形 worker 化、时间线波形取绑定源、retime 片段绑定登记后续片，见 ledger §5）；优先级：P1；性质：核验后补完整源素材流程。证据：[audio sync ledger](validation/feature_F/F2.10/ba6da523f/audio_sync_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F2.1、F3.1。
- 条件依赖：F2.6：长录制漂移分析；F3.3：采样级修正；不要求先具备实时多机位网格。
- 入口：EUI media_pool/source_viewer；HOST edit_controller；EDIT 资产/源绑定与 commands，复用 multicam_sync 的相关算法和 metadata，不能把 MulticamAngle.sync_offset 当成外录音素材模型。
- 实施：盘点是否已有“摄影机视频＋独立 WAV→同步源素材”入口；补 TC/波形/手动确认、追加或替换源音轨、保留机内参考声、多声道与 offset 的身份/持久化。同步绑定走单次领域事务，与普通链接 A/V、多机位切角、播放时钟同步区分。
- 验收/交付：外录多声道 WAV 提前开录，Source Viewer 预听、插入/修剪、保存重开及成片保持同一关系；无共同开头音频可用 TC/手动。低置信度、错 TC、采样率不同时不误绑，取消零写入；原视频/WAV 不被破坏性改写。

## Phase F3 · 音频精度与预览/导出一致性

<a id="f3-1"></a>

### F3.1 统一音频图与嵌套语义复核

- [x] 状态：已完成（复核确认三路径同源闭环；补齐：子转场丢弃诊断、Deliver 入队前嵌套音频 BLOCKING/WARNING 预检（消除任务运行才失败）、预览 diagnostics 信号化、freeze 门/fade 存活测试；父 retime 重采样与 mixed-fps 重映射登记后续差量并注明 AV 两侧一致约束）；优先级：P0；性质：复用/补强。证据：[nested semantics ledger](validation/feature_F/F3.1/203e239ec/nested_semantics_ledger_2026-10-23.md)。
- 依赖：F1.1、F0.4。
- 入口：EDIT audio_graph_snapshot/audio_graph_renderer/audio_retime、timeline_export、nested flatten 与 commands。
- 实施：确认预览、成片、Stem 共用源时间映射、clip FX/automation、freeze、crossfade、routing；补父 retime、子混合 fps、子转场、双层嵌套语义及 resolver 传递。已修复项只保留证据，不再造简化音频渲染分支。
- 验收/交付：正弦/脉冲与真实对白覆盖反放/曲线变速、非零入点、trim/split/Undo；NTSC 相邻样本边界不留洞。嵌套音频/转场被正确保留，暂不支持的组合在 preflight 明确拒绝。

<a id="f3-2"></a>

### F3.2 流式实时保调变速

- [x] 状态：已完成（2026-10-24；倍率突变保留对齐模板/1x 同路两处有意偏离与速度曲线/transport 倍率后续片见 ledger §3/§4）；优先级：P1；性质：补齐实时能力。证据：[streaming preserve pitch ledger](validation/feature_F/F3.2/streaming_preserve_pitch_ledger_2026-10-24.md)。
- 依赖：F3.1。
- 入口：EDIT audio_playback/audio_timestretch/audio_retime；capability_registry 与速度 UI。
- 实施：选择可流式的伸缩实现，明确延迟/窗口/声道/速度范围和许可证；复用离线语义，音频回调中不做无界分配/解码。seek、loop、暂停和倍率突变清理时域状态，与视频时钟补偿协同。
- 验收/交付：0.5/1.25/2 倍语音与已知频率向量，时长/音高落在实施前冻结的算法容差；预听/导出策略一致，开关提示准确，切换无异常脉冲/爆音并有真实监听记录。

<a id="f3-3"></a>

### F3.3 采样级音频编辑与迁移

- [x] 状态：已完成（2026-10-24；完整采样域时间线边界移除与子帧 crossfade 登记后续片见 ledger §3/§4）；优先级：P1；性质：模型/工具建设。证据：[sample time ledger](validation/feature_F/F3.3/sample_time_ledger_2026-10-24.md)。
- 依赖：F0.4、F3.1。
- 入口：EDIT timeline_v2/commands/audio graph；PROJECT 子 schema；EUI timeline/属性/波形。
- 实施：引入有采样率/有理数语义的子帧位置与 fade，不用浮点帧叠加；视频保持帧对齐。盘点 ripple/linked/trim/split、markers、速度、交换、保存对整数帧的假设，逐个兼容迁移。
- 验收/交付：48kHz 可移动/修剪一个 sample；非整数 fps 长序列无累积漂移；旧工程迁移、Undo、Stem/成片、跨平台重开精确保留；旧版不支持时给兼容说明，不能静默舍入新字段。

<a id="f3-4"></a>

### F3.4 路由、自动化、响度、Stem 与录音

- [x] 状态：已完成（2026-10-24，三片：`1bbadd9e3`/`834bb9fcb`/片3；核验为主+六缺陷修复+sidecar；后续片见 ledger §5）；优先级：P1；性质：补强。证据：[routing loudness stems ledger](validation/feature_F/F3.4/routing_loudness_stems_ledger_2026-10-24.md)。
- 依赖：F3.1、F1.1。
- 条件依赖：F3.3：采样级编辑联验；F3.2：实时变速场景；普通路由/Stem 不以这些扩展为前置。
- 入口：EDIT audio DSP/graph、loudness_analyzer/meter、recording/ADR 现有入口、delivery_qc。
- 实施：先核实已有 Bus/Send/自动化/BWF/Stem；覆盖 FX 关键帧真实 PCM 求值、平滑、延迟/路由与 pre/post-fader 规则。响度保留声道语义、静音保护与 true peak；分析走可取消 worker，取消不提交结果。surround/auto-duck 先核实际消费者，再按用户需求接入。
- 验收/交付：mono/stereo/反相/不相关及声明支持的多声道向量；Master Stem 与成片在相同边界一致，分轨重组按明确路由重建混音。监听/录音设备切换、长录制、取消与异常及时关闭资源。

## Phase F4 · 多镜头调色与参考管理

<a id="f4-1"></a>

### F4.1 场景分组与组前/片段/组后处理

- [x] 状态：已完成（2026-10-24，提交 ef0993564；复用既有双层+共享语义，新增组实体/命令/微调栈/链解析单点/四路消费；后续片见 ledger §4）；优先级：P1；性质：核验后差量建设。证据：[color group ledger](validation/feature_F/F4.1/ef0993564/color_group_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 入口：PROJECT color_stack/topos_project；COLOR ColorSession/渲染组合；EDIT UseSharedGradeCommand 等既有共享语义；HOST layer/edit controller。
- 实施：先核素材默认/clip override/shared grade，设计场景/机位组与明确处理顺序；保留单镜头曝光微调，共享 look 与 input/output transform 分开。增加/移组/解组通过领域命令，明确多时间线引用和缓存失效。
- 验收/交付：同场景至少 12 镜头、两类机位共同改 look，而各镜头微调仍保留；移组/复制/删除/Undo/重开/导出一致；组前后顺序有画质向量，源→工作与输出变换不重复。若已有等价能力，验证后复用，不强制仿造节点 UI。

<a id="f4-2"></a>

### F4.2 命名调色版本与共享 look 锁定

- [x] 状态：已完成（2026-10-24；两处 pre-existing 缺陷修复与 40 处构造器清理登记见 ledger §3/§4）；优先级：P1；性质：核验后建设。证据：[color versions ledger](validation/feature_F/F4.2/color_versions_ledger_2026-10-24.md)。
- 依赖：F4.1、F0.4。
- 入口：PROJECT color stack/manifest；COLOR session/grade_clipboard/预设 UI；EDIT shared grade commands。
- 实施：设计独立命名版本、活动版本和共享依赖身份；共享 look 默认防误改，显式解锁/复制独立版本；切换不覆盖其他镜头私有修改。保存版本含参数/效果/色彩语义/插件版本，工程级版本留给 F8.1。
- 验收/交付：同镜头 A/B/C 一键比较/回退；跨镜头共享变化的影响范围可预览；复制/删除版本、Undo、保存恢复和非当前序列导出取正确版本。插件缺失不丢版本数据，明确不可渲染原因。

<a id="f4-3"></a>

### F4.3 Lightbox、Gallery 与 HDR 参考 still

- [x] 状态：已完成（2026-10-24，基础 Gallery/HDR still 独立验收路径；F4.2 版本对比条件依赖已满足；后续片见 ledger §后续）；优先级：P1；性质：升级。证据：[hdr still ledger](validation/feature_F/F4.3/hdr_still_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F4.2：调色版本对比；基础 Gallery/HDR still 可先独立验收。
- 入口：COLOR preset_meta、presets_panel、canvas/reference；HOST layer_controller；PROJECT/现有 float 图像设施。
- 实施：保留 JPEG 浏览兼容；另外保存具有工作空间/传递函数/曝光与显示语义的高精度参考，选用已有可支持的 float 图像存储并声明版本。多镜头 lightbox 复用有界缓存与调色后帧，支持 wipe/split/difference 和锁定显示变换。
- 验收/交付：超 1.0 高光、负值、渐变/alpha 在高精度参考中保留；旧 JPEG 标明精度限制而非伪称 HDR 参考；批量 rename/delete/移动/缺 sidecar 有一致行为。缩略图不冒充全精度对比，UI/示波器使用同一声明域。

<a id="f4-4"></a>

### F4.4 批量镜头匹配与人工修正

- [x] 状态：已完成（2026-10-24；模型/命令/守卫闭环，批量预览 UI 与分布规格化登记后续片）；优先级：P2；性质：核验后接通。证据：[shot match ledger](validation/feature_F/F4.4/shot_match_ledger_2026-10-24.md)。
- 依赖：F0.4、F5.1。
- 条件依赖：F4.1：场景组批量提交；F4.2：命名版本；F4.3：持久化 still。源镜头匹配不等待全部管理工具。
- 入口：COLOR ai/color_context、现有 Color Match/grade commands；AI snapshot/quality gate；HOST layer_controller。
- 实施：盘点已有 ShotMatchWorkflow 的真实消费者；先做可解释曝光/白平衡/分布匹配建议，再评估模型。锁定参考版本/源解释，保护已锁镜头/品牌色；建议批量预览，低置信度保留人工确认，统一提交并支持逐镜头修正。
- 验收/交付：多机位同场景匹配与肤色/中性色基准检查；剪影、极端光色、HDR 不被强行拉成同分布。过时 revision 拒绝、取消零写入、单次/逐项 Undo 和导出一致；声明算法适用边界。

<a id="f4-5"></a>

### F4.5 二级调色参数与遮罩动画覆盖

- [x] 状态：已完成（2026-10-24；Qualifier 亮度区间先行，其余域显式静态；后续片见 ledger §4）；优先级：P1；性质：能力矩阵核验/最小扩展。证据：[qualifier keyframes ledger](validation/feature_F/F4.5/qualifier_keyframes_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F5.1。
- 条件依赖：F7.1：跟踪与手工动画混用；F4.2：调色版本之间的动画复制。
- 入口：COLOR core/grade_keyframes 的 GRADE_KEYFRAME_PARAMS/MASK_KEYFRAME_FIELDS、Qualifier/curve/warper UI、mask/tracking；HOST layer_controller 的参数记录路径；既有曲线控件。
- 实施：区分基础 grade 动画、Qualifier/曲线/Warper/插件参数、窗口变换与点集拓扑。已有 Mask 变换不能说成完全没有 Power Window；不支持的域当前明确标静态。先选一个真实必要参数（如 Qualifier 亮度区间）贯通记录/求值/曲线/逐参数复制/静态切换，再按证据扩展；不默认给离散字段/任意插件全部插值。
- 验收/交付：人物从阴影走向高光时限定器随帧变化，随机 seek/反放/非零入点/变速/Undo/重开/成片取一致值；跟踪与用户动画的组合顺序明确。点集动画若扩展，另定义拓扑和插值兼容；未经定义的字段不得由 ◆ 暗示支持。

<a id="f4-6"></a>

### F4.6 HDR 分区创作工具与高光塑形

- [x] 状态：已完成（2026-10-24 核验收口——现有 soft_clip 两区工具覆盖验收句，分区轮 UI 条件延期如实记录）；优先级：P2；性质：需求核验后的小范围创作工具建设。证据：[hdr zones ledger](validation/feature_F/F4.6/hdr_zones_ledger_2026-10-24.md)。
- 依赖：F5.1、F0.4。
- 条件依赖：F4.5：新增参数动画；F5.2：监看设备联验。不把 F5 的色彩管理/HDR 输出当作已具备 HDR 创作分区。
- 入口：COLOR ui/color_wheel、core/layer 的 grading/soft_clip、shader/参数 schema；CM 与 EFFECT。已有 builtin.hdr 工作区仅是面板组合，先核现有工具能否满足具体镜头。
- 实施：有实际 HDR 镜头需求时明确 scene-linear、曝光 stops/亮度域和分区边界/rolloff，先实现最少几区的 exposure/色彩调整；与 qualifier 空间局部选区分开。优先复用既有运算，避免为仿造界面重写整个调色引擎。
- 验收/交付：保留中间调肤色而调整明亮窗户/高光区域，跨分区无不连续色阶；负值/超 1.0、黑位、色域行为有定义，已支持 CPU/GPU/成片逐项对拍。无对应需求可记录“现有工具覆盖/条件延期”，不能宣称已实现分区轮。

## Phase F5 · 色彩管理、监看与相机素材

<a id="f5-1"></a>

### F5.1 输入—工作—显示—输出色彩闭环

- [x] 状态：已完成（审计定位真缺口=域而非公式：三路 SDR 常数已逐份一致；本批 CPU OETF 三处内联收口到 provider 单点、GPU 四处拷贝冻结常数合同测试、OCIO 形状脆弱降级暴露并加固形状契约、三路语义表显式声明；HDR 预览统一（IDT+tonemap 抽共享模块）登记 F5.2 候选）；优先级：P0；性质：核验/补强。证据：[color pipeline ledger](validation/feature_F/F5.1/43e00f850/color_pipeline_ledger_2026-10-23.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F4.1：新增组前/组后颜色链；现有基础色彩闭环先验收。
- 入口：CM；COLOR color_manager/shader/uniform；MEDIA 色彩标签；EDIT preview/export；DELIVER 配置/manifest。
- 实施：按格式声明输入解释、工作空间、view/display 与 output transform；区分 metadata override 和实际像素转换。涵盖 PQ/HLG/Rec.709/P3、full/limited、线性 alpha、proxy/original 和 bake/output。先用参考色块/梯度向量和固定 OCIO 配置，再上真实相机素材。
- 验收/交付：CPU/GL/已启用 Metal/D3D 路径各有对拍；避免重复 tone map/gamma/range；导出文件实际像素/bit depth/标签相符。Scope/false color/zebra 用参考向量核单位与采样位置：nits、IRE、code value、scene-linear 与 PQ/HLG 不能混标，显示 LUT 前后行为须明示。容差按运算/位深冻结，scene-linear 超范围不得无理由裁剪；硬件缺失保留待验证行。

<a id="f5-2"></a>

### F5.2 校准监看与实际输出质量

- [ ] 状态：未开始；优先级：P1；性质：设备核验后建设。
- 依赖：F5.1、F0.1；仅 GPU 直送子项依赖 A/B 对应实际实现。
- 入口：VIDEO monitor_output；HOST file_controller；Color/ProgramViewer 监看消费；已有 DeckLink native bridge。
- 实施：区分 UI 显示变换与外接监看变换；明确输出模式、帧率、bit depth、range、色彩/alpha、声画时序及校准 LUT 作用位置。保留可用 CPU 路径，GPU 直送只继承 A/B 的真实能力；不重复建设、也不因 CPU 路径就认定质量不合格。
- 验收/交付：在对应监看设备上做已知图案/高光/渐变、真实素材和持续输出；记录输出格式/校准条件/实测或反馈证据。未有硬件则设备子项未验收；不能由截图或提交接口返回成功推断 10bit/HDR 正确。

<a id="f5-3"></a>

### F5.3 相机 RAW 与媒体支持矩阵

- [x] 状态：已完成（2026-10-24；BRAW/R3D SDK 与 CinemaDNG 序列按计划门槛登记后续片不冒称）；优先级：P1；性质：核验后按格式建设。证据：[media matrix ledger](validation/feature_F/F5.3/media_matrix_ledger_2026-10-24.md)。
- 依赖：F5.1、F0.4。
- 入口：MEDIA format_registry/factory/ffmpeg_source、既有 RAW 后端；COLOR raw_panel/image_engine；工程素材 metadata。
- 实施：把扩展名分类、容器打开、实际解码、源级 RAW 控制、代理/seek、导出分别列能力；按真实目标相机/版本选择 BRAW/R3D/CinemaDNG 等具体格式，不承诺一口气全部支持。新增 SDK 先核许可证、分发、平台 ABI 与取消/释放；照片 RAW/Topos RAW 单列。
- 验收/交付：每个承诺格式有真实原片，源级白平衡/曝光/去拜耳设置对预览和成片产生预期结果；重开和代理切换保留解释，SDK 缺失给诊断与转码建议。文件扩展名已识别不算验收完成。

<a id="f5-4"></a>

### F5.4 常见输入解释与通用图像序列导入

- [x] 状态：已完成（2026-10-24 两片；序列插入消费链/端到端几何对拍/EXR-DPX 按需求登记后续片）；优先级：P1；性质：媒体输入支持矩阵核验/差量建设。证据：[input interpretation ledger](validation/feature_F/F5.4/input_interpretation_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F5.1。
- 条件依赖：F2.7：用户解释覆盖；F6.6：代理/缓存联验；F6.4：图像序列往返输出。基础输入不可等待输出格式扩展。
- 入口：MEDIA ffmpeg_source/metadata/factory、现有 topos_image_sequence；EDIT timeline_frame_mapper/timeline_v2 的 PTS index；EUI media_pool 导入/解释入口。
- 实施：先独立验证 VFR PTS 索引构建与生产绑定、reverse 的策略、隔行 TFF/BFF 保留或去场、SAR/display matrix/rotation 与用户 Transform 的组合；不能把 metadata 字段存在当支持。再做单帧/序列显式选择、首帧号/零填充、fps、缺帧和 fingerprint；先明确 PNG/TIFF 子集，EXR/DPX 按需求验证位深/alpha/AOV/颜色。
- 验收/交付：手机 VFR 长片音画不漂移，90°片/非方像素/隔行片在 Source→Edit→代理→导出方向/几何/时刻一致，未支持组合可拒绝/转码。shot_1001 序列作为一个资产、缺 1007 帧有诊断，trim/重开/移盘帧号与 alpha 一致；不能强制把连号照片合为序列。

## Phase F6 · 专业交接、重套与通用交付格式

<a id="f6-1"></a>

### F6.1 FCPXML/OTIO 支持子集与外部往返

- [x] 状态：已完成（2026-10-24 核验后补强；受支持子集先往返验收为计划允许路径）；优先级：P1；性质：补强。证据：[interchange roundtrip ledger](validation/feature_F/F6.1/interchange_roundtrip_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F3.3：新增子帧 schema 映射；F4.2：新增调色版本映射。当前受支持子集可先往返验收。
- 入口：ADAPTER；EDIT interchange/capability_registry；EUI interchange_dialog；既有 import builder/command compiler。
- 实施：冻结可交换子集，建立外部 NLE 生成的黄金工程；核对 reel/source timecode、混 fps、声道、marker、转场、速度、嵌套和调色信息。扩展 preserved/converted/dropped/requires_user_action 报告，不能由旧表误关已可用 adapter。
- 验收/交付：至少一条真实软件往返逐切点/源帧身份核验；不支持的复杂效果/速度明确 bake/flatten/drop 或取消，不能静默丢失。OTIO 依赖未安装为未验证；导入走事务和 Undo。

<a id="f6-2"></a>

### F6.2 回原片 reconform、代理重连与归档

- [x] 状态：已完成（2026-10-24 核验+匹配器交付；LUT 收集/对话框接线/外部 reconform 登记后续片）；优先级：P1；性质：补强。证据：[relink archive ledger](validation/feature_F/F6.2/relink_archive_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F6.1：外部 NLE reconform；本地代理/原片重连与归档不等待交换扩展。
- 入口：PROJECT offline_asset_manager；EDIT project_archive/media 资产服务；EUI relink_media_dialog；现有 Vault 指纹/重连能力。
- 实施：优先复用 fingerprint、源时码、卷名/时长等匹配；多候选先人工确认，不能仅同名就替换。代理/原片分辨率、裁剪、时间基、色彩解释和源入点均纳入重套验证；归档依赖包含外部 LUT/字体/插件声明及可重建资源边界。
- 验收/交付：移盘/改根路径后成片切点、源帧与调色不变；重名异片不误连；变速/嵌套重套有正确映射。取消/失败原工程保留，缺项报告可操作，不把本机绝对路径写成可移植依赖。

<a id="f6-3"></a>

### F6.3 AAF 声音后期交接

- [ ] 状态：条件延期（2026-10-24；两验收门未开见 ledger §1，复开条件 §3——按计划原文此条在接收端验收前不得标完成）；优先级：P1；性质：新增受控能力。证据：[aaf gate ledger](validation/feature_F/F6.3/aaf_gate_ledger_2026-10-24.md)。
- 依赖：F6.1、F3.1、F3.4。
- 条件依赖：F3.3：新增采样级编辑 schema 的交换；按实际支持子集声明。
- 入口：ADAPTER 协议/报告、EDIT interchange/能力表、音频源映射；拟新增 AAF adapter（开工确认文件名）。
- 实施：先选一个真实接收软件与最小配置；验证库/许可证和媒体封装。支持轨道、源时码、声道、handles、位置/采样时间和声明的音量自动化；复杂总线/插件/retime 明确烘焙或报告，不以 Stem/CSV 冒充可重建时间线。
- 验收/交付：真实接收软件导入能重建剪辑和声道/时间关系，handles 可修剪；缺媒体、格式不支持和取消有诊断。接收端未验收保持未完成；OMF 不自动扩项，保留明确不可用声明。

<a id="f6-4"></a>

### F6.4 PNG/TIFF 等通用图像序列输出

- [x] 状态：已完成（2026-10-24 核验+manifest 恢复语义）；优先级：P2；性质：核验后补覆盖。证据：[frame manifest ledger](validation/feature_F/F6.4/frame_manifest_ledger_2026-10-24.md)。
- 依赖：F5.1、F1.1。
- 入口：DELIVER job/preflight/runner；EDIT timeline_export；MEDIA 既有 Topos Image Sequence；COMP 图像输出。
- 实施：先核当前格式限制，复用已有效果/渲染路径；分批支持具体 PNG/TIFF 位深、alpha、颜色标签和帧号约定。写临时产物后确认，恢复依据已验证帧 manifest，不只看文件名存在。
- 验收/交付：外部合成软件回导帧数/帧号/alpha/色彩一致；暂停、取消、缺帧、损坏帧、断电恢复不跳过坏文件、不混不同配置。Topos 自有序列继续兼容。

<a id="f6-5"></a>

### F6.5 素材入库、组织与受控校验拷贝

- [x] 状态：已完成（2026-10-24 核验+受控拷贝核；UI 入口/多目的/真卡现场登记后续片）；优先级：P1；性质：已有组织流程验收/入库差量建设。证据：[verified ingest ledger](validation/feature_F/F6.5/verified_ingest_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F1.2。
- 条件依赖：F2.10：外录同步素材组织；F5.4：通用序列输入；摄影卡 verified ingest 子项按真实现场备份需求立项，不阻塞普通链接导入。
- 入口：EUI media_pool 的文件/文件夹导入、SmartBin/搜索/评级/关键词；EDIT project/commands/project_archive 的拷贝与 SHA-256；现有 Vault/shared 资产 facade。不得让 feature 直接反向依赖 apps 层。
- 实施：先核 Bin/Smart Bin、文件夹映射、metadata 与路径去重已有能力，补导入结果、重复/重名冲突、失败重试和批量组织验收。需安全入库时先做单目的地受控拷贝→源/目标完整校验→成功登记，保持摄影卡目录/卷名/相机/scene/shot metadata，复用已有后台/归档校验；快速指纹不冒充整文件拷贝完整性。
- 验收/交付：新增素材正确更新动态集合，批量组织 Undo/重开有效；同名异卡、不完整文件、目的盘断开/取消/重试有逐项报告，未验证文件不能标成功，不覆盖良好拷贝。源卡只读；多目的备份可后续扩展，不把 archive hash 当已具备 ingest 用户流程。

<a id="f6-6"></a>

### F6.6 项目代理与渲染缓存管理流程

- [x] 状态：已完成（2026-10-24 核验+安全清理；UI 入口/目录迁移面板登记后续片）；优先级：P1；性质：已有服务/入口验收与补强。证据：[proxy cache ledger](validation/feature_F/F6.6/proxy_cache_ledger_2026-10-24.md)。
- 依赖：F0.1、F1.1、F1.2。
- 条件依赖：F5.4：特殊输入；F6.2：代理/原片重连；F7.3：合成/RIP 资源管理。基础管理不等待这些扩展。
- 入口：MEDIA asset_proxy_manager/proxy_manifest/disk_proxy_service；EUI media_pool 代理操作；现有 preferences 的 Render Cache 设置；EDIT render_cache；COMP 既有缓存设施。
- 实施：盘点生成/删除代理、格式/目录/分辨率/容量设置已存在的入口，再补当前项目源/代理/cache 实际使用状态、队列/失败原因/重试、按工程/选中/失效清理、目录迁移和低磁盘策略。复用既有 manager/键与引用生命周期，不另造平行管理器或侵占 A/B 的性能改造。
- 验收/交付：批量生成取消/换工程/失败重试可解释；播放中清理正确失效/回退，效果变化不会播放旧 cache。活动文件/跨项目引用受保护，不能误删原片、归档或其他工程必需文件；位置/配额变化后任务与占用统计一致，导出使用原片/缓存的策略可见。

## Phase F7 · 合成与时域效果的真实镜头质量

<a id="f7-1"></a>

### F7.1 跟踪、Roto、抠像与修补质量

- [x] 状态：已完成（2026-10-24 核验为主+镜头集补齐；发丝镜头/COMP 增量重算/人脸接线登记后续片）；优先级：P1；性质：质量核验/定向补强。证据：[tracking quality ledger](validation/feature_F/F7.1/tracking_quality_ledger_2026-10-24.md)。
- 依赖：F0.3、F1.1、F5.1。
- 入口：COLOR tracking/tracker UI；COMP tracker/roto/keyer/paint/修补节点、分析 worker 和缓存。
- 实施：以遮挡、运动模糊、透视、细发丝/半透明和镜头切换建镜头集；核漂移/置信度/失败检测，增加手动锚点修正、分段重跟、失效重算。已有点/平面/人脸/AI 工具逐一确认真实覆盖，不能按节点数宣布画质等价。
- 验收/交付：有标注特征/遮罩参考，实施前冻结像素误差/边缘/时域容差；失败不污染后续帧，UI 可修正且重开/Undo 生效。Composite→Edit→导出实际镜头一致，模型不可用可解释。

<a id="f7-2"></a>

### F7.2 去噪、补帧与运动相关时域一致性

- [x] 状态：已完成（2026-10-24 核验为主+补帧一致性；RIFE 模型在位评估与主观观察记录登记后续片）；优先级：P1；性质：质量补强。证据：[denoise interpolate ledger](validation/feature_F/F7.2/denoise_interpolate_ledger_2026-10-24.md)。
- 依赖：F7.1、F0.4。
- 入口：COLOR denoise/export_denoise_bridge；COMP frame_interpolate/时域节点；共享帧窗口与缓存。
- 实施：定义历史窗口、预热、seek/切镜/速度/分辨率变化的重置规则；缓存键包含模型/参数/源解释和时间采样。对降噪细节损失/鬼影、补帧遮挡/重复帧、运动模糊快门语义分别评估，不用单一 PSNR 覆盖主观质量。
- 验收/交付：顺序、随机 seek、冷/热缓存与最终导出符合声明的时域定义；重复渲染按随机种子可复现，切镜不拖影。输出可客观对比并有真实观察记录；无法实时则明确代理/缓存策略，不偷降交付质量。

<a id="f7-3"></a>

### F7.3 复杂节点图、缓存与 Render-in-Place

- [x] 状态：已完成（2026-10-24 核验+烘焙源签名；UI 徽标与真实工程基准登记后续片）；优先级：P2；性质：补强。证据：[rip cache ledger](validation/feature_F/F7.3/rip_cache_ledger_2026-10-24.md)。
- 依赖：F7.1、F7.2、F1.2。
- 入口：COMP graph/evaluators/render/cache/RIP；EFFECT；PROJECT composition payload；Edit 合成片段适配。
- 实施：用现有典型复杂图测交互、dirty cone、缓存命中和取消；优先修实际瓶颈。烘焙需绑定图版本/源 hash/时间域/格式，可恢复原图；嵌套、alpha、AOV 和 retime 有支持说明。
- 验收/交付：参数变化只重算受影响子树，内存/显存有界；缓存坏/模型缺失/后台烘焙取消可恢复。RIP 与实时/导出的受支持输出一致，更新原图时不悄悄播放旧烘焙；不为扩节点数而增加未接线注册项。

## Phase F8 · 工程版本、候选镜头与审片

<a id="f8-1"></a>

### F8.1 命名工程快照与版本比较

- [x] 状态：已完成（2026-10-24 模型层闭环；比较面板 UI/自动备份调度/IO 接线登记后续片）；优先级：P2；性质：核验后建设。证据：[project snapshots ledger](validation/feature_F/F8.1/project_snapshots_ledger_2026-10-24.md)。
- 依赖：F1.2、F0.4。
- 条件依赖：F4.2：新增调色版本依赖；基础工程命名快照不等待调色版本 UI。
- 入口：PROJECT 模型/io/manifest；EDIT 事务/revision；现有工程管理 UI。
- 实施：区分自动备份、命名项目版本与调色版本；快照记录依赖和来源 revision，采用现有包/内容寻址机制。比较片段移动/修剪/替换、效果/调色/音频变化，初版先只读比较与完整恢复，不急做任意三方合并。
- 验收/交付：命名版本可重开、复制分支和回退；原片移动/插件缺失仍保留版本元数据并报告依赖；比较结果与真实差异一致，不复制巨大临时缓存或 secret，不污染当前 Undo。

<a id="f8-2"></a>

### F8.2 带版本锚点的审片评论与 marker

- [x] 状态：已完成（2026-10-24 模型层闭环；审片面板 UI 与 marker 命令接线登记后续片）；优先级：P2；性质：核验后建设。证据：[review comments ledger](validation/feature_F/F8.2/review_comments_ledger_2026-10-24.md)。
- 依赖：F8.1、F0.4。
- 入口：EDIT markers/事务；PROJECT；既有审片/导入/AI 工件接口（F0.2 先查实际消费者）。
- 实施：定义评论身份、作者、状态、版本 hash、精确时间基/源帧/片段锚点；先做文件导入导出和评论→marker，支持处理/回复/状态，不默认部署云服务。剪辑变更后的重定位须给置信度，失效锚点不能静默挪到别的镜头。
- 验收/交付：客户第 N 版评论定位正确；重复导入幂等；改 fps/重套/删除镜头后可解释映射或标失效；Unicode、时间码、隐私清理、Undo/保存恢复有效。

<a id="f8-3"></a>

### F8.3 多 take 候选/Audition

- [x] 状态：已完成（2026-10-24 模型层闭环；UI 面板/工程字段/预览消费接线登记后续片）；优先级：P2；性质：核验后建设。证据：[audition ledger](validation/feature_F/F8.3/audition_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1。
- 条件依赖：F8.1：工程版本比较；F2.2：高级长度冲突规则。基础候选切换可先验收。
- 入口：EDIT 片段/命令/源映射；EUI 选择与预览；PROJECT 子 schema。
- 实施：先查通用多 take 是否已有；候选容器保留各源范围/音频/效果，定义不同长度候选的放置规则、活动 take 与预览行为。与 ADR 音频试听、调色版本区分，不把所有备选做成破坏性替换。
- 验收/交付：同位置切换不同长度 take，时间线/邻片变化可预览；Undo、保存重开、成片取正确候选；交换明确 flatten，缺源不丢其他候选。

## Phase F9 · AI 用户任务与交付自动化

<a id="f9-1"></a>

### F9.1 文稿驱动粗剪与删除口头语

- [x] 状态：已完成（2026-10-24 核验地基+补检测/计划缺口；UI 选中入口与跨容器映射登记后续片）；优先级：P2；性质：核验后接通。证据：[filler removal ledger](validation/feature_F/F9.1/filler_removal_ledger_2026-10-24.md)。
- 依赖：F0.4、F3.1、F1.1。
- 条件依赖：F2.1：源多声道选择；F3.3：逐采样编辑精度；首版可声明帧对齐边界。
- 入口：AI 转录/analysis/context/snapshot/edit_api/工作流；EDIT canonical commands；字幕/marker 适配。
- 实施：确认逐词时间戳与源/时间线映射；选词删除先给 linked A/V、字幕、marker 的差异预览，再经既有事务提交。保留口头语人工排除、低置信度和混合语言；批量重试以 plan/hash 和输入 revision 约束，不反复删同段。
- 验收/交付：文稿操作、画面、声音、字幕与 marker 一致；跨 split/retime/嵌套的支持范围准确。过时计划拒绝、取消无半提交、一次 Undo 可恢复；模型不可用有清晰诊断，不能只返回一个编辑计划就称已完成。

<a id="f9-2"></a>

### F9.2 多语言字幕、AI 质量门与可恢复批处理

- [x] 状态：已完成（2026-10-24 核验+翻译后 QC；画布重排/自动断行登记后续片）；优先级：P2；性质：补强。证据：[subtitle quality ledger](validation/feature_F/F9.2/subtitle_quality_ledger_2026-10-24.md)。
- 依赖：F0.4、F1.1、F1.3。
- 条件依赖：F9.1：文稿联动编辑。
- 入口：AI workflow/execution_policy/quality_gate/artifact store；EDIT captions/import/export/QC；DELIVER 字幕策略。
- 实施：复用转录/翻译/字幕；镜头匹配的提交与验收归 F4.4，不是字幕/翻译的前置条件。任务显式绑定目标工程、版本、轨道和模型。长任务有检查点与结果校验，缺模型/断网/取消不提交残缺成果；支持人工修词、断句与风格锁。遵循已有授权流程，不在此计划中自动开启付费模型或外部发送。
- 验收/交付：多语言、字体缺失、逗号/标签/换行、ASS/TTML 时间基、行长/阅读速率/重叠 QC 覆盖；16:9→9:16/1:1 的字形、断行、安全框与 burn-in 最终像素正确，跨 Mac/Windows 的字体依赖按 F2.9/F6.2 处理。burn-in/sidecar 取正确语言/版本；重试不重复 cue，Undo/保存/导出一致。质量门有来源证据和失败原因，不仅模型自评。

<a id="f9-3"></a>

### F9.3 多版本交付、QC 与任务恢复

- [x] 状态：已完成（2026-10-24；核验+编译器/PCM 缺口；UI 入口/manifest 接线登记后续片）；优先级：P1；性质：补强。证据：[multiversion delivery ledger](validation/feature_F/F9.3/multiversion_delivery_ledger_2026-10-24.md)。
- 依赖：F1.1、F1.2、F5.1。
- 条件依赖：F3.4：高级音轨/Stem；F6.4：图像序列输出；F8.1：命名快照；F9.2：AI 生成/翻译的多语言版本。普通多任务交付与 QC 不等待 AI 功能。
- 入口：DELIVER job/preflight/queue/runner；EDIT delivery_qc/timeline_export；已有输出探针/manifest。
- 实施：用同一冻结工程版本产生分辨率、画幅、声轨/Stem、字幕组合，命名冲突和素材依赖先检查。复用 QC 增强响度/true peak、黑/静帧、缺帧、metadata 检查；有意黑场/静帧可标注豁免。重试/恢复验证源版本、配置及输出完整性。
- 验收/交付：每份成片与声明的版本/范围/颜色/声轨/字幕对应，实际解码/PCM/QC 有报告；取消/重启不覆盖成功输出，不用旧版本缓存续渲新版本；关键失败给失败状态/退出语义而非只写日志。

## Phase F10 · 条件扩展（显式立项，不能算作核心已完成）

<a id="f10-1"></a>

### F10.1 多人协作、锁定与冲突

- [ ] 状态：未开始；优先级：P3；性质：需求/原型通过后建设。
- 依赖：F8.1、F8.2、F1.2；另需真实多人同改需求和服务/权限方案。
- 入口：PROJECT 事务/revision/存储；既有身份/共享工件接口。具体服务/数据库新增前写 ADR。
- 实施：先局域/共享存储只读与写入租约，再评估实时协作；定义超时、续租、fencing、离线写入和版本冲突。媒体路径/缓存按用户独立，不把文件同步当项目事务合并。
- 验收/交付：两人同改不会静默覆盖；断网/过期客户端无法越权覆盖新版本，明确冲突选择及审计。权限、备份、协议迁移与真实并发场景验收；无需求则延期并保留本地快照/评论路线。

<a id="f10-2"></a>

### F10.2 VST3/AU 音频插件宿主

- [ ] 状态：未开始；优先级：P3；性质：需求/兼容性评估后建设。
- 依赖：F3.2、F3.3、F3.4、F1.2；另需 SDK/许可证、进程架构与打包方案。
- 入口：既有 audio effect registry/graph/状态持久化。原生插件宿主作为窄接口接入，不在 Python 实时回调里承载插件扫描/执行。
- 实施：分开处理插件扫描隔离、进程/线程模型、延迟补偿、状态/预设、实时/离线、GUI 和架构兼容；先约定真实插件小矩阵。插件崩溃/缺失时提示并保留状态，不能静默旁路后仍称导出一致。
- 验收/交付：声明支持的真实插件在预听/成片/Stem 结果及延迟一致，重开恢复状态；扫描/执行崩溃不损工程，绕过/恢复可解释。AU 仅列适用平台，不承诺跨平台同插件等价。

<a id="f10-3"></a>

### F10.3 广播/影院与高级 HDR 交付

- [ ] 状态：未开始；优先级：P3；性质：按实际客户标准立项。
- 依赖：F5.1、F9.3。
- 条件依赖：F5.2：需要实际监看设备的特定客户标准；依具体规范选择。
- 入口：DELIVER preflight/编码/封装/QC、CM、字幕/音频链。
- 实施：按真实项目逐个选择封装、字幕、音频布局、HDR metadata/trim 或影院交付，不同时承诺全部标准。先出支持/不可支持/第三方依赖矩阵，再写最小端到端实现与验证器。
- 验收/交付：真实接收方/验证工具通过，规范要求与输出逐项匹配；未经所需认证/授权不宣传认证兼容。无对应用户需求则延期，现有通用交付继续可用。

<a id="f10-4"></a>

### F10.4 大型 3D/高级合成扩展

- [ ] 状态：未开始；优先级：P3；性质：镜头需求驱动。
- 依赖：F7.1、F7.2、F7.3；另需真实镜头证明现有 3D/跟踪/AOV 不能完成任务。
- 入口：COMP 现有 3D、场景/AOV/跟踪与 EFFECT，不默认新增平行场景图。
- 实施：按具体镜头评估相机解算、空间/渲染能力或外部软件互操作；先对接一个受控场景，再扩大模型/材质/动画支持。估算数据量、渲染与持久化边界，优先保留已有稳定工作流。
- 验收/交付：目标镜头从导入、修正、缓存到 Edit/成片可完成，AOV/alpha/时间语义正确；模型格式/规模/后端支持矩阵清楚。无明确需求时不以扩节点数量立项。

<a id="f10-5"></a>

### F10.5 第三方视频效果插件宿主（如 OpenFX）

- [ ] 状态：未开始；优先级：P3；性质：真实插件需求驱动的条件扩展。
- 依赖：F1.2、F1.3、F5.1、F7.3。
- 条件依赖：F4.5：第三方参数动画。自有 .tglsl/.tai 或 DCTL 包装不自动代表兼容 OFX 二进制插件。
- 入口：COLOR plugin_manager、EFFECT、现有插件参数/预览/导出能力与状态持久化。扫描当前安装/源码后，SDK/许可/原生宿主边界再写 ADR；本轮仅未找到成熟 OFX 生产入口，不断言所有外部插件都绝对不可用。
- 实施：只对一款有需求的真实插件先验证扫描隔离、实例/状态、pixel domain/alpha、时域取帧、GPU/CPU、多线程与设备能力；不在 UI/脚本线程直接执行 GPU 插件。未知插件保留参数/身份并显式诊断，不能静默旁路输出。
- 验收/交付：参数/动画、Undo/重开、预览/成片一致；缺插件/版本不兼容/崩溃保留工程状态且失败可解释；能力声明精确到已验证插件/平台。无真实需求延期，不阻塞自有插件和核心调色。

<a id="f10-6"></a>

### F10.6 远程渲染与可移植交付任务

- [ ] 状态：未开始；优先级：P3；性质：有多机生产需求后立项。
- 依赖：F1.2、F6.2、F9.3。
- 条件依赖：F10.1：多人服务器/权限体系复用；远程渲染与多人实时编辑可独立评估。
- 入口：DELIVER 现有队列/job snapshot/manifest、PROJECT 包/归档与能力探测。B 计划中 C 农场只是跑测试/基准，不是产品已有远程渲染服务；当前源码检索未确认后者。
- 实施：先设计冻结版本、素材/字体/LUT/插件/模型依赖和 worker 环境校验，再做单 worker 受控派单。身份认证、取消/心跳、幂等 job id、输出校验/回收、安全日志均须定义；机器本地 GPU handle 不能序列化跨机复用。
- 验收/交付：另一台声明兼容的机器输出同一版本任务，缺依赖/后端不兼容先拒绝或明确降级；掉线/重试不生成重复有效交付、不覆盖已完成输出，客户端仍能查看状态/取消。无需求延期，不作为 F9 普通本地队列的前置。

## 3. 阶段验收与回退

| 检查点 | 核心退出条件 | 未通过时 |
|---|---|---|
| M0（F0） | A/B 已收口，能力差量、黄金工程、身份/时间/迁移契约可复现 | 停在核验，修证据/接口，不能直接开大功能 |
| M1（F1） | 关键历史项当前不复现或已修；保存/恢复/跨页/成片闭环正确 | 内容/状态损失先修；缺硬件的专项保持未验证 |
| M2（F2/F3） | 修剪、视频变速、手工标题、外录同步、多机位及声音任务可由真实入口完成，模型/PCM/成片一致 | 回滚最小任务，不靠 UI 禁用掩盖已声明支持的错误 |
| M3（F4/F5） | 整场景调色、二级动画、版本/参考、常见输入与色彩链有证据；HDR 分区按明确需求验收或延期 | 关闭新入口/路由，保留原 grade/颜色配置和工程数据；设备缺失的子项保持未验证 |
| M4（F6/F7） | 素材组织/代理缓存、外部往返/reconform/AAF 等支持范围已验收，困难镜头质量达标；摄影卡校验入库按需求单列 | 缩到已验证子集，未支持部分显式报告/延期 |
| M5（F8/F9） | 版本、评论、AI 提交和多版本交付可恢复、可撤销、可追溯 | 隔离新 schema/任务，禁止半成品覆盖已完成工程/输出 |
| M6（F10） | 仅验收已立项的扩展，其余保持延期且不声明能力 | 不阻塞已验收核心版本，不能说“全部扩展完成” |

本表的 ☐/任务复选框用于人工勾选；关闭时须同步状态和证据链接，不能只打勾。

M2 等阶段名表示验收集合，**不要求为等待跨阶段依赖而死守文档顺序**：例如先做 F3.3 才验收 F2.6 的采样矫正，先做 F3.2 才声明 F2.2 的实时保调填充。按任务 DAG 调度，未完成子能力不能被阶段打勾掩盖。

每项交付必须有最小可回滚变更、配置/能力开关与工程兼容路径。渲染/音频/异步 worker 必须覆盖取消、失败与窗口关闭；不能靠进程退出/GC 兜底证明清理完成。数据迁移不承诺可无损降版：回退旧应用前保留原工程备份，新版本字段继续保留或明确拒绝打开。

## 4. 历史问题回归索引 H（26 项，不预判仍有 bug）

以下编号来自旧交互审计；**全表初始状态为待复测，不是待重写**。F1.1 是当前正确性门禁；后续 F 项只扩展功能/覆盖，避免把历史修复重复记工作量。

| 旧 ID | 必须重放的要点 | 当前核验/未来责任任务 |
|---|---|---|
| C01 | Transform 负/大位移与旋转范围；remove/Undo | F1.1；F2.4 |
| C02 | 新轨/已有轨、链接/多选移动时动画局部时域 | F1.1；F2.4 |
| C03 | 跨轨转场快照，Undo 恢复邻接内容 | F1.1；F2.3 |
| C04 | 链接 A/V 变速后音频转场原子 Undo | F1.1；F3.1 |
| C05 | Freeze/split 后 markers、尾转场与源映射 | F1.1；F3.1 |
| C06 | 锁轨效果/关键帧操作零提交 | F1.1；F2.4 |
| D01 | 反放/曲线/倍率音频与非零入点映射 | F1.1；F3.1、F3.2 |
| D02 | 保留声道的归一化、反相/静音保护 | F1.1；F3.4 |
| D03 | 长音频分析后台化、取消与过时结果 | F1.1；F3.4 |
| D04 | clip FX 关键帧真正影响 PCM | F1.1；F3.1、F3.4 |
| D05 | Stem 与成片的 FX/freeze/crossfade/retime | F1.1；F3.1、F3.4 |
| D06 | 29.97/59.94 音频相邻样本边界与总样本数 | F1.1；F3.1、F3.3 |
| D07 | 音频/stretcher 的成功/取消/异常显式 close | F1.1、F1.2；F3.2 |
| D08 | ASS/SSA 逗号、标签、换行与往返 | F1.1；F9.2 |
| D09 | TTML tick/frame/clock/倍率/嵌套 begin | F1.1；F9.2 |
| D10 | 容器/音频/alpha/HDR 契约 enum/string 一致 | F1.3；F5.1、F9.3 |
| U01 | 真实 sequence 的自定义/In-Out/字幕/嵌套范围 | F1.1；当前已有 effective duration 修复，先复测 |
| U02 | 全量子序列 resolver 贯通 preflight 与 worker | F1.1；当前已有 map 接线，先复测；扩展 F3.1 |
| U03 | 非当前序列草稿/context/队列引用正确 | F1.1；F9.3 |
| U04 | Next/Prev/Home/End/seek 后可见四端同帧 | F1.1；F2.3 |
| U05 | 字幕查找/定位的画面、播放头与命中行 | F1.1；F9.2 |
| U06 | 无截图切源、GL/generation/租约/关闭压力 | F1.1、F1.2；继承 A/B 资源协议 |
| U07 | 链接/markers 等已可用入口的文案真实 | F1.3 |
| Q01 | decoder/container close 顺序与单进程套件退出 | F1.1、F1.2、F1.3 |
| Q02 | nested preflight 的完整模型夹具和真实宿主 | F1.1、F0.3 |
| Q03 | 旧 interchange helper 与权威能力一致 | F1.3；F6.1 |

## 5. 建议覆盖索引（本轮九方向＋旧 G01–G17）

| 来源 | 对应工作包 | 收口说明 |
|---|---|---|
| 本轮 1：工程/预览/成片可靠性 | F0、F1、F9.3 | 不重复改已修历史项 |
| 本轮 2：剪辑/多机位深度 | F2.1–F2.10；F1.4 | 实时预览、变速、标题、源/目的/外录音频与效率分层验收 |
| 本轮 3：音频精度/保调/Stem | F3；F10.2 | 插件宿主是条件扩展 |
| 本轮 4：多镜头调色、版本与 HDR still | F4 | 已有 shared grade/Gallery/wipe 优先复用 |
| 本轮 5：色彩、监看、相机 RAW | F5；F10.3 | 不将 A/B 延期硬件能力算已完成 |
| 本轮 6：交换与回原片 | F6.1–F6.6；F5.4 | 交换有外部接收软件验证；输入、入库和代理管理各自闭环 |
| 本轮 7：合成、跟踪、时域镜头质量 | F7；F10.4 | 节点数量不代替镜头质量 |
| 本轮 8：工程版本/审片/协作 | F8；F10.1 | 自动保存、快照、评论、多人并发各有边界 |
| 本轮 9：AI/多版本/QC/可恢复批处理 | F9 | 真实提交/可撤销，纯计划对象不算完成 |
| G01 / G02 / G03 | F2.1 / F2.2 / F2.3 | 源映射 / 三四点 / 专业修剪 |
| G04 / G05 | F2.5 / F2.6 | 实时多机位 / 同步与漂移 |
| G06 / G07 | F3.2 / F3.3 | 实时保调 / 采样级编辑 |
| G08 / G09 | F6.3 / F6.1、F6.2 | AAF / 受控往返和 reconform |
| G10 / G11 / G12 | F9.1 / F8.3 / F8.1、F8.2、F10.1 | 文稿剪辑 / 候选 take / 版本审片协作 |
| G13 / G14 | F2.7 / F6.4 | 批量属性 / 通用图像序列 |
| G15 / G16 / G17 | F10.2 / F2.4 / F3.1 | 插件 / 普通属性动画 / 嵌套音频转场与管理 |

G17 的父子导航/展开也在 F1.1 的真实嵌套工程中核验；若当前入口缺失，先以最小领域命令补齐导航/展开语义与 Undo，不能仅修音频就关闭该项。对文件/模块“未找到入口”的建议，必须经过 F0.2 后再判定实施差量。

## 6. 委派、评审与每包交付模板

可交 ZCode 的适宜工作：只读调用链/引用/能力清单、日志与 skip 聚类、素材/测试索引、已定契约下的 focused test 和局部实现。先验证小任务能用正确模型/配置回收结果，再扩大委派；CLI 未回传或模型未确认时不得假装已完成复核。

任务提示必须包含：绝对项目/文件路径、PyQt6+ModernGL+GLSL 背景、`.clinerules`、任务 ID、允许读写范围、依赖/当前证据、禁止操作、测试与交付格式。共享模型/时间基/色彩/资源协议的设计、外部软件验收和合并判断由主控复核；多 agent 不能同时改同一共享文件。

```text
任务：F?.?（复制本表该任务的范围；一次只实施一个可验证子包）
起点：实际 base/head、A/B 接续记录、依赖任务的验收记录。
文件：展开源码别名为绝对路径，列白名单和只读调用者。
过程：核验现实现→确认最小差量→补必要行为测试→实现→独立审查→验收。
边界：遵循 .clinerules；不得绕过 command/service；不改无关工作区；
      不把未验证的模型/设备/外部格式标为可用；不自动发布或执行本表其他任务。
交付：实际变更、用户路径、schema/迁移、Undo/保存/预览/导出、失败/取消/关闭证据；
      focused test 命令/退出码、真实媒体/设备/外部软件结果、性能回归、未验证项、回滚。
关闭：有证据才更新任务状态；延期列阻塞和替代路径，不计功能完成。
```

结构性变更同步更新对应架构文档和产品能力表。文档/脚本示例必须能映射到已实现入口；拟新增的文件/CLI 明确标注，完成实现后再填命令。核心功能阶段不因 Token 剩余而扩范围，也不以 Token 配额代替成本/收益估算。

### 6.1 先复用的回归入口

下列文件在编制时存在，开工重新定位；只运行本包受影响子集并补必要测试，不每包默认全量。既有测试通过只覆盖它实际验证的层级，G/R/H/X 另按正文执行。

| 工作包 | 已有 focused test 起点（相对 TEST） |
|---|---|
| F1 | `deliver/test_video_export_preflight.py`、`project/test_project_module_preservation.py`、`app/test_project_save_routing.py` |
| F2 | `edit/test_trim_professional.py`、`edit/test_multicam_production.py`、`edit/test_multicam_nested_timecode_truepeak.py` |
| F3 | `edit/test_audio_timestretch.py`、`edit/test_audio_graph_parity.py`、`edit/test_audio_leftover_samples.py`、`edit/test_nested_audio_flatten.py` |
| F4 | `color/test_color_session.py`、`color/test_grade_clipboard.py`、`color/test_grade_keyframes.py` |
| F5 | `shared/color/test_color_transform_provider.py`、`color/test_color_transform_provider_parity.py`、`shared/test_monitor_output.py` |
| F6 | `edit/test_interchange.py`、`edit/test_interchange_ui.py`、`edit/ai_editing/test_fcpxml_adapter.py`、`edit/ai_editing/test_otio_adapter.py`、`media/test_topos_image_sequence_export.py` |
| F7 | `color/test_tracker_mask_binding_flow.py`、`video_denoise/test_temporal_denoise_state.py`、`composite/test_composite_project_migration.py` |
| F8/F9 | F0.2 先查当前版本/评论/AI 用户入口的真实测试；基于上述工程保留、字幕、命令/音频测试增加对应端到端覆盖，不编造不存在的测试文件 |

已有隔离回归示例（未来开工使用，本次未执行）：

```bash
cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01"
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python -m pytest tests/deliver/test_video_export_preflight.py tests/project/test_project_module_preservation.py -q --no-header -p no:cacheprovider
```

Windows CMD 对应示例（在实际项目目录，不假设复制 Mac venv）：

```bat
set "QT_QPA_PLATFORM=offscreen"
set "TOPOS_HEADLESS_GL=1"
.venv\Scripts\python.exe -m pytest tests\deliver\test_video_export_preflight.py tests\project\test_project_module_preservation.py -q --no-header -p no:cacheprovider
```

以上仅为隔离测试进程的环境。真实窗口验收需使用正常平台配置，不能继承 offscreen 并声称已验上屏/监听/监看。测试失败先归因基线/环境/新增回归，不把 failed 改 skip 获取全绿。

## 7. 开工时第一批工作

1. 完成 A/B 后，由 F0.1 接收产物并创建可复现基线记录。
2. F0.2 全量差量核验；F0.3 黄金工程与 F0.4 契约可在清楚文件所有权后并行。
3. 用 F1.1 先关闭仍复现的关键正确性问题；F1.2/F1.3 建恢复和能力门禁。
4. F3.1/F5.1 建立共用语义基线，再按 Studio 的 F2/F3 或 Color 专注版的 F4/F5 推进；阶段号不是串行锁。每次只派一个小任务，按硬依赖推进，条件依赖只门禁对应子能力。
5. 每包验收后更新总表和证据 ledger；每阶段结束复跑黄金工程与受影响 A/B 基准，重算下一包范围。

## 8. 第二轮遗漏复核与范围纠偏

本轮发现的是**计划遗漏或验收边界不完整**，不能直接等同于产品完全没有相应功能。复核采用源码入口、消费者与既有测试检索，尚未执行这些用户流程的真机验收。原 42 项编号保留，增至 **53 项：9 个定向核验/补强包、2 个条件扩展包**；已有能力先核验，按差量实施。具体证据需在 F0.2 随开工版本更新。

### 8.1 新增工作包与依据

下列路径沿用 §2 的绝对目录别名；字段/函数存在只证明相应实现片段，不能单独证明完整用户流程可用。

| 任务 | 当前源码证据与判断 | 原计划遗漏的最小边界 |
|---|---|---|
| F1.4 快捷键/工作区/控制台 | `src/shared/ui/shortcut_manager.py` 已有预设、自定义与冲突报告，`shortcut_scope.py` 管作用域；HOST `color_workspace_controller.py`、COLOR `integrations/tangent_bridge.py` 已有工作区/设备链路 | 新动作接入、文字焦点/浮窗、多屏恢复、设备反馈与连续 Undo 的联验；无需另建系统 |
| F2.8 视频变速插值 | EDIT `clip_params.py::SpeedParams` 已有速度点/插值字段；EUI `edit_properties_panel.py` 当前隐藏未可用的 optical_flow；COMP 补帧节点不自动构成 Edit 变速支持 | nearest/frame_blend 的完整验收，以及按需求补时间线光流 provider、质量/缓存/导出闭环 |
| F2.9 手工标题/模板 | EUI `edit_effects_panel.py::_populate_titles` 已有多类标题；EDIT `title_templates.py` 与 `captions/graphics.py::CaptionGraphicBinding` 已有模板/同步机制 | 手工图形独立于 AI 的模板生命周期、时长适配、样式继承和跨平台字体验收 |
| F2.10 独立录音同步 | HOST `edit_controller.py::_on_multicam_sync_requested` 面向至少两个角度；EDIT `multicam_sync.py` 已有角度同步算法，本轮未定位到完整的单视频＋独立 WAV 源素材生产闭环 | TC/波形/手动同步后作为可预听、可插入和持久化的源素材，保留参考声与声道身份 |
| F4.5 二级调色/遮罩动画 | COLOR `core/grade_keyframes.py` 的 `GRADE_KEYFRAME_PARAMS` 主要覆盖基础调色；`MASK_KEYFRAME_FIELDS` 覆盖部分变换；HOST 参数记录存在域限制 | 逐参数可动画矩阵、Qualifier 等必要参数贯通；点集拓扑另立契约，不泛称完全没有遮罩动画 |
| F4.6 HDR 分区创作 | COLOR `ui/color_wheel.py` 的 LGG、`core/layer.py` 的 grading/soft_clip 已存在；`core/workspace_presets.py` 的 builtin.hdr 是布局，不能据此证明有亮度分区轮 | 真实 HDR 镜头需求下的分区边界/rolloff、曝光与高光塑形；与 HDR 色彩管理/输出分开 |
| F5.4 常见输入/序列导入 | MEDIA `ffmpeg_source.py` 探测几何/时间 metadata；EDIT `timeline_frame_mapper.py` 的 VFR 映射需要 PTS index，本轮未确认普通导入完整绑定；MEDIA `factory.py` 的 Topos 序列入口不等于 PNG/TIFF 连号资产入口 | VFR/隔行/SAR/旋转的实际消费及导出一致性，单图/序列选择、缺帧和帧号模型；区别于 F6.4 输出 |
| F6.5 入库/素材组织 | EUI `media_pool_panel.py` 已有文件夹导入、评级/关键词/Smart Bin、路径去重；EDIT `project_archive.py` 已有拷贝及 SHA-256 核验 | 先补已有组织流程验收；真实摄影卡需求下做“校验成功才登记”的入库闭环，归档校验不能冒充现场入库 |
| F6.6 代理/缓存管理 | EUI `media_pool_panel.py` 已有代理操作；MEDIA `asset_proxy_manager.py`、`disk_proxy_service.py` 与既有 Render Cache 偏好设置已提供基础设施 | 项目状态、失败重试、目录/配额、失效/安全清理与跨项目引用保护；A/B 负责性能，本项负责用户流程 |
| F10.5 OFX 等第三方宿主 | COLOR `core/plugin_manager.py` 与 `src/features/shared/effects/dctl_wrapper.py` 已有自有插件/DCTL 链路，本轮未找到成熟 OFX 生产宿主入口 | 有真实插件需求后，逐插件核 SDK/许可证、隔离、像素/时域/状态/GPU 能力；保持条件扩展 |
| F10.6 远程渲染 | DELIVER 已有本地队列/冻结任务设施；本轮检索未确认跨机 worker 生产服务，B 的测试农场不是产品渲染服务 | 有多机生产需求后补可移植依赖、受控 worker、幂等恢复与输出回收；本地交付独立完成 |

### 8.2 已有能力的验收补项，不另算新功能

- F0.3 增加 Smart Bin 动态更新、Adjustment Clip 作用范围/顺序、多时间线身份与跨线复制的黄金场景；这些已有实现不能再次按“新增模块”计工作量。
- F5.1 增加 Scope/false color/zebra 的单位、参考向量与采样位置校验；已有 `scope_types.py` 类型声明不能替代实际数值验收。
- F2.9/F9.2 复用已有安全框与 EDIT `captions/font_resolver.py`，增加横竖画幅、字体/断行和最终像素验收；字体归档/缺依赖报告衔接 F6.2。

### 8.3 依赖与实施范围修正

原表 15 项把仅影响部分场景的能力列成了整包前置。本版拆分硬依赖与条件依赖，并同步总表；例如普通多版本交付不必等待 AI 字幕，字幕翻译不必等待镜头匹配，本地归档/重连不必等待所有外部交换扩展，基础色彩验证也不必等待新组调色。条件子项只有实际验收后才能声明支持，不能借拆依赖把缺项算完成。

HDR 分区创作、摄影卡校验入库和第三方/跨机扩展先以真实镜头或生产需求决定范围。与已经受支持的基础输入、保存、声音、色彩、手工标题和本地交付相比，它们不应仅因对标目标而优先扩大工程量。本表是可接续的实施范围，仍不能承诺穷尽 Resolve 所有版本/硬件/工作流能力。

复核工作流范围参考官方 [Media：素材管理、同步、校验拷贝与网络渲染](https://www.blackmagicdesign.com/products/davinciresolve/media)、[Edit：视频变速插值与编辑工作流](https://www.blackmagicdesign.com/products/davinciresolve/edit/)、[Color：HDR 分区调色](https://www.blackmagicdesign.com/products/davinciresolve/color)。这些页面说明对标工作流；Topos 的当前能力判断以本项目源码和后续 G/R/H/X 验收为准。

## 修订记录

- 2026-10-22 v1：依据当前功能评估、A/B v3 接续要求和旧交互审计编制。覆盖本轮九方向、旧 G01–G17 建议及 26 项历史回归；当前状态全部未开始/待复测。仅编制计划，未实施功能。
- 2026-10-22 v2：再次对照源码补 9 个定向核验/补强包及 2 个条件扩展包，42→53 项；补已有能力的黄金场景、Scope/字体/画幅验收，拆分 15 项硬/条件依赖并修正启动顺序。原编号及 26 项历史回归保留；本次只更新计划，功能状态仍全部未开始。
