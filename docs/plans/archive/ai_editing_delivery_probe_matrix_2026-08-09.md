# AI Editing 导出交付探针与真实素材矩阵

日期：2026-08-09
范围：P8 导出交付与质量门禁、P10 真实素材/故障门

## 本批结论

- 宿主视频编码完成回调已接入 post-export hard gate：编码线程只有在 manifest、首帧缩略图、ffprobe、hash 和 `DeliveryQualityGate` 全部通过后才发出成功信号。
- 探针失败不会因为返回了“看似合格”的字段而放行；`MediaProbeResult.succeeded` 现在是独立硬条件。
- 对 `tests/test_videos` 的 9 个真实素材执行了项目 `FfprobeMediaProbe` 矩阵：9/9 成功打开并取得分辨率、帧率、时长、视频编码和像素格式。
- 本批扩展了真实探针输出：9/9 同时取得封装、文件大小和视频码率；这些字段现在可直接用于平台交付合同，而不是只停留在报告层。
- 新增 provider-neutral 平台 profile 与字幕合同硬门：平台可校验画幅、帧率、视频编码、封装、时长、文件大小、码率、音频采样率和音频存在性；字幕可校验时间范围、重叠、空文本、阅读速度、单行长度和显式安全区 bbox。
- 宿主完成回调自动收集启用字幕轨的 `SubtitleClip`，并在 `job.succeeded` 之前将平台/字幕结果写入 manifest；未提供平台 profile 时保持兼容，提供 profile 但缺少必要测量时 fail closed。
- 静音策略已产品化为两个显式模式：默认 `measure_only` 只测量并记录，`hard_max` 必须同时配置最大静音秒数和有效测量，否则在导出成功前阻断；有意留白不会被默认策略误判为失败。
- 新增真实 4K 编码基准：短样本输出写入临时目录，H.264/HEVC/ProRes 422 HQ/DNxHR HQX 均在编码后重新 ffprobe，另测 H.264+HEVC 双 worker 并发。
- 字幕安全区现在使用最终字幕纹理的真实 bbox：复用轨道默认样式、cue 覆盖、字号、CJK/Latin 字形以及缩放/旋转/平移/pivot 后的四角包围盒；宿主在编码前生成 title-safe/action-safe/platform/custom 矩形并接入现有质量门。
- 修复 Qt 字幕 bottom 对齐没有执行 8% 底部留白的问题，使 Qt 预览/烧录与 PIL fallback 的 base pose 一致；不再因为渲染路径差异产生安全区漂移。
- 品牌元素交付合同已闭合：`logo`、`end_card`、`disclaimer` 等要求可声明最短展示帧数和安全区测量；缺失元素、时序越界、展示不足、bbox/safe-area 缺失或越界都会阻断，并将脱敏结果写入 manifest。
- 品牌要求已提升为可持久化的 `EditorialBrief` 合同：brief 序列化/恢复和 revision patch 都会校验要求；宿主导出会按当前 active brief 自动合并要求与安全区配置，临时导出输入仍可显式覆盖。
- 真实 4K 全源长度样本已完成：以 `4K_H264.mov` 的 29.72 秒有效时长执行四编码单 worker，编码后全部重新 ffprobe；另保留 20 秒双 worker H.264+HEVC 并发数据。
- 多源真实长样本并发已完成：`4K_H264.mov` + `4K_H265.mov` 两条约 29.72 秒 4K 源执行 H.264/HEVC 四任务、双 worker，`4/4` 成功且全部重新 ffprobe；源级预检每条源只执行一次，单源失败不会取消其他源任务。
- 真实终态回调验收使用 `2K_h264.mp4` 复制到临时输出目录，完成 manifest/thumbnail/ffprobe/hash/质量门全链路：`job=succeeded`、`quality.passed=true`、manifest 与 thumbnail 均存在。原始测试素材未被修改。

## 真实素材探针矩阵

本矩阵为真实容器探针测试；为避免对多 GB mezzanine 文件重复读完整 hash，矩阵探针关闭 hash，生产导出完成路径保持默认 hash 开启。

| 素材 | 分辨率 | fps | 时长（秒） | 视频编码 | 像素格式 | 音频 |
|---|---:|---:|---:|---|---|---|
| 2K_DNxHR HQX 12-bit.mov | 1920×1080 | 25/1 | 29.720 | dnxhd | yuv422p12le | pcm_s24le / 48 kHz |
| 2K_H265.mov | 1920×1080 | 25/1 | 29.720 | hevc | yuv420p10le | pcm_s24le / 48 kHz |
| 2K_h264.mp4 | 1920×1080 | 25/1 | 62.891 | h264 | yuv420p | aac / 48 kHz |
| 2K_prores422HQ.mov | 1920×1080 | 25/1 | 25.880 | prores | yuv422p10le | pcm_s16le / 48 kHz |
| 4K_DNxHR HQX 12-bit.mov | 3840×2160 | 25/1 | 29.720 | dnxhd | yuv422p12le | pcm_s24le / 48 kHz |
| 4K_H264.mov | 3840×2160 | 25/1 | 29.720 | h264 | yuv420p | pcm_s24le / 48 kHz |
| 4K_H265.mov | 3840×2160 | 25/1 | 29.720 | hevc | yuv420p10le | pcm_s24le / 48 kHz |
| 4K_prores422HQ.mov | 3840×2160 | 25/1 | 29.720 | prores | yuv422p10le | pcm_s16le / 48 kHz |
| 4K_prores4444.mov | 3840×2160 | 25/1 | 10.440 | prores | yuv444p12le | pcm_s16le / 48 kHz |

新增平台测量字段（真实 ffprobe）：

| 素材 | 容器 | 文件大小（bytes） | 视频码率（kbps） | `horizontal_social` |
|---|---|---:|---:|---|
| 2K_DNxHR HQX 12-bit.mov | mov | 690270064 | 183500.8 | 阻断：编码/码率 |
| 2K_H265.mov | mov | 32018173 | 6311.5 | 通过 |
| 2K_h264.mp4 | mov/mp4 | 52513775 | 6548.2 | 通过 |
| 2K_prores422HQ.mov | mov | 519105531 | 158927.1 | 阻断：编码/码率 |
| 4K_DNxHR HQX 12-bit.mov | mov | 2714083188 | 728268.8 | 阻断：分辨率/编码/码率 |
| 4K_H264.mov | mov | 53743212 | 12161.0 | 阻断：分辨率 |
| 4K_H265.mov | mov | 38460323 | 8045.6 | 阻断：分辨率 |
| 4K_prores422HQ.mov | mov | 2667990735 | 716628.9 | 阻断：分辨率/编码/码率 |
| 4K_prores4444.mov | mov | 1434246484 | 1096699.9 | 阻断：分辨率/编码/码率 |

这里的 `horizontal_social` 是项目内置的 1920×1080 交付 profile，不宣称任何供应商当前政策；供应商或客户规格应通过自定义 `PlatformDeliverySpec` 覆盖。2K H.265/H.264 通过，4K 和 mezzanine 编码被明确阻断，证明平台门不是“编码进程成功即放行”。

## 已覆盖的故障门

- 输出不存在、ffprobe 不可用、超时、非零退出和非法 JSON；
- 探针返回 `succeeded=false` 但质量字段伪装合格；
- manifest 或 thumbnail 路径缺失；
- 缩略图生成失败、manifest 写入失败；
- 输出分辨率、帧率、时长、音频采样率以及输出 hash 不符合质量契约；
- `VideoExportWorker` 在 post-check 失败时只发 error，不发 finished。

## 实现接缝

- `FfprobeMediaProbe(include_output_hash=False)` 支持大素材快速矩阵测量；生产导出默认仍计算 SHA-256。
- `ExportService.complete_host_export()` 生成脱敏 manifest 和首帧 JPEG，再调用 `finalize_export_with_probe()`。
- `VideoExportWorker` 在编码成功与 `finished` 信号之间执行 post-export 回调，因此 UI 不会先看到一个未经验证的“导出成功”。
- manifest 不保存输出路径；job 对外序列化仍沿用既有脱敏规则。
- `expected_lufs` 与 `lufs_tolerance` 是显式导出策略：未配置时只报告，配置后要求响度后端可用且 integrated LUFS 在容差内。
- `delivery_contracts.py` 提供 provider-neutral `PlatformDeliverySpec`、内置横/竖/方形 profile 和自定义 JSON profile；`FfprobeMediaProbe` 将封装、文件大小、视频码率同时写入 `MediaProbeResult.metadata` 与 `DeliveryQualityInput`。
- `validate_subtitle_cues()` 接受 `SubtitleClip`、`SubtitlePlacement` 或 JSON-safe cue shape；宿主导出自动将启用字幕轨交给校验器，失败结果进入既有 `DeliveryQualityGate` 和 manifest。
- `measure_subtitle_render_bbox()` 复用 `captions.render_plan` 的 canonical affine，只测量四角，不分配全画布；`MainWindowAiEditHostBridge` 用与 `TimelineExporter._burn_in_subtitles()` 相同的 `resolve_style(track_default ⊕ cue_override, base=clip_style_base)` 生成质量 cue。
- `build_subtitle_safe_area()` 提供 `title_safe`、`action_safe`、横/竖/方平台别名和 custom inset；bbox/safe-area 非有限、形状非法或尺寸倒置时按安全门阻断。
- `BrandElementRequirement` / `validate_brand_elements()` 接受 JSON-safe 的实测品牌元素记录（`element_id`、`kind`、帧范围、bbox、safe_area），并由 `ExportService.finalize_export_with_probe()` 将结果合并到 `DeliveryQualityInput` 与 `delivery_contracts.brand_elements`。宿主导出会给缺省 safe_area 的品牌测量记录补上当前 profile 矩形；这是一条可审计的测量接缝，不冒充从任意未知素材自动识别 Logo。
- `materialize_brand_elements_from_timeline()` 会读取素材 `metadata.brand_elements` 的显式标注，把 source frame range 映射到启用视频轨的 timeline frame range，并将归一化 canvas bbox 转为输出像素；未标注素材不猜测，坏标注保留为 invalid measurement 并由质量门阻断。
- `EditorialBrief.brand_requirements` 是持久化输入合同；`BriefService.apply_patch()` 负责 revision bump 与结构校验，`MainWindowAiEditHostBridge` 在导出预检前从 active brief 合并要求和 `safe_area_profile`，因此品牌门不会只依赖一次性 UI 参数。
- `encode_benchmark.py` 使用固定非 shell ffmpeg 参数、超时和临时目录，返回脱敏的 codec/耗时/文件大小/实时倍速/重开结果，不会污染测试素材或工程输出。
- 多源编码 benchmark 在任务并发前缓存每条源的 4K 预检结果，并在报告中记录 `source_count`；任务级错误隔离，所有输出仍位于临时目录并随上下文退出清理。

## 本批回归记录

- 实现提交：`4cc613f6`（多源编码源级预检缓存、source_count 报告字段和任务错误隔离）；前置声明式素材品牌物化为 `aeb33786`，EditorialBrief 品牌要求持久化为 `65c6a4d6`，品牌元素时序/bbox/safe-area 合同为 `2b735a94`，字幕 bbox/Qt-PIL 修复为 `15761ce7`，前置静音/4K 基准为 `193cca77`）。
- `tests/edit/ai_editing`：`1220 passed / 2 skipped`。
- 真实素材矩阵：`10 passed`（9 个媒体元数据探针 + 1 个 H.264 帧/音频分析）。
- 本批品牌合同/质量门回归：`34 passed`；宿主桥接与品牌测量规范化：`10 passed`。
- 本批 EditorialBrief 持久化、revision patch、会话恢复与宿主读取回归：`43 passed`。
- 本批品牌标注物化、source→timeline 映射、坏标注 fail-closed 与宿主自动接缝回归：`18 passed`。
- 全 `tests/app`：`203 passed / 2 failed`；失败均在未改动的 `edit_controller` 时间线管理测试桩，错误是 `_TimelinePanel` 缺少既有 `set_project`，不涉及本批导出/质量门代码。
- 额外全量 `tests/composite`：`4398 passed / 26 failed / 6 skipped`；失败集中在既有 UI、Schema 版本和交互断言，未涉及本批字幕文件。全量 `tests/edit` 在 macOS VideoToolbox 能力探针后台线程处触发环境级 segmentation fault，未将未完成的长尾结果宣称为通过。

## 真实 4K 多编码端到端基准

基准输入为 `tests/test_videos/4K_H264.mov`，每个编码取真实 4K 画面的 3 秒，所有结果写入临时目录；编码结束后逐项重新 ffprobe，分辨率必须与源一致。以下是单 worker 实测：

| 编码 | 成功 | 编码耗时（秒） | 实时倍速 | 输出大小（bytes） |
|---|---:|---:|---:|---:|
| H.264 | 通过 | 2.240 | 1.339× | 1185702 |
| HEVC | 通过 | 2.615 | 1.147× | 338680 |
| ProRes 422 HQ | 通过 | 5.737 | 0.523× | 131339918 |
| DNxHR HQX | 通过 | 4.017 | 0.747× | 273105159 |

单 worker 总墙钟 `14.625s`，四项 `4/4` 通过，平均实时倍速 `0.939×`。双 worker 并发 H.264 + HEVC 总墙钟 `3.706s`，`2/2` 通过，平均实时倍速 `0.813×`。这证明了真实编码、容器写出和 post-encode reopen 接缝，但仍是 3 秒短样本，不等价于 4K 长片持续编码性能。

本批补充全源长度样本：输入 `tests/test_videos/4K_H264.mov` 实测有效时长 `29.720s`，请求 `30s`，单 worker 总墙钟 `94.993s`，四项 `4/4` 通过且输出均重新 ffprobe。

| 编码 | 编码耗时（秒） | 有效时长（秒） | 实时倍速 | 输出大小（bytes） | post-ffprobe |
|---|---:|---:|---:|---:|---:|
| H.264 | 10.579 | 29.720 | 2.809× | 10022522 | 通过 |
| HEVC | 16.924 | 29.720 | 1.756× | 2984120 | 通过 |
| ProRes 422 HQ | 51.307 | 29.720 | 0.579× | 1611442667 | 通过 |
| DNxHR HQX | 16.105 | 29.720 | 1.845× | 2705542902 | 通过 |

这组数据覆盖一条真实 4K 源的全时长编码与重开，但不代表所有 4K 编码器、素材内容和长片时长的持续吞吐；20 秒 H.264+HEVC 双 worker 的墙钟为 `17.157s`，`2/2` 通过，平均实时倍速 `1.260×`。

本批补充多源全时长并发：输入为 `4K_H264.mov` 与 `4K_H265.mov`，各实测有效时长约 `29.720s`，请求 `30s`，双 worker 总墙钟 `59.136s`，四项 `4/4` 通过且输出均重新 ffprobe。

| 源 | 编码 | 编码耗时（秒） | 有效时长（秒） | 实时倍速 | 输出大小（bytes） | post-ffprobe |
|---|---|---:|---:|---:|---:|---:|
| 4K_H264.mov | H.264 | 21.570 | 29.720 | 1.378× | 10022522 | 通过 |
| 4K_H264.mov | HEVC | 28.286 | 29.720 | 1.051× | 2984120 | 通过 |
| 4K_H265.mov | H.264 | 27.920 | 29.720 | 1.064× | 8228307 | 通过 |
| 4K_H265.mov | HEVC | 30.648 | 29.720 | 0.970× | 2914837 | 通过 |

多源并发平均实时倍速为 `1.116×`；该结果证明源级预检缓存、任务级失败隔离和多源临时输出清理有效，但不代表更大源数量、更多 codec 或更长商业片的持续吞吐。

## 并发探针基准与故障矩阵

使用 `benchmark_media_probe()`、`max_workers=2` 对上述 9 个真实素材做元数据探针（关闭矩阵 hash，避免把磁盘吞吐误算成探针吞吐）：

| 指标 | 实测值 |
|---|---:|
| worker 数 | 2 |
| 样本数 | 9 |
| 成功/失败 | 9 / 0 |
| 总墙钟时间 | 0.503 秒 |
| 单项平均 | 104.372 ms |
| P50 | 95.730 ms |
| P95 | 150.862 ms |

`run_delivery_fault_matrix()` 已提供 8 类确定性发布前故障报告入口，实测 8/8 阻断：输出缺失、provider 句柄未解析、分辨率不匹配、损坏帧、未知许可、manifest 缺失、缩略图缺失、字幕重叠。

## 真实帧级与音频分析

使用 `FfmpegDeliveryAnalyzer(sample_fps=1.0)` 对 9 个真实素材完成实际解码采样：视频层 9/9 分析成功，黑帧、冻结帧、损坏帧均为 0。音频层结果保留了真实风险：

| 素材分组 | 音频测量 |
|---|---|
| 2K/4K DNxHR HQX、2K/4K H.265、2K/4K ProRes 422 HQ、4K H.264 | 峰值为 `-inf dB`，最长静音约等于素材时长，需复核素材音轨内容 |
| 2K H.264 | 峰值 `+2.903 dB`，最长静音 `0.2s`；导出质量门正确阻断“音频存在削波风险” |
| 4K ProRes 4444 | 峰值 `-11.436 dB`，最长静音 `0s` |

BS.1770/LRA 实测补充：2K H.264 为 `-17.845 LUFS / 14.735 LU`，4K ProRes 4444 为 `-27.527 LUFS / 1.707 LU`；其余 7 个样本 integrated LUFS 为 `-70.0` floor，和长静音测量一致。所有样本的响度后端均可用。目标 LUFS 现在是显式可选硬门，例如目标 `-23 LUFS ±1 LU` 会拒绝这些明显偏离或静音素材，但未配置目标时只写入报告。

这次真实闭环确认了两件事：视频解码健康不等于音频可交付；`complete_host_export()` 已将帧级计数和峰值测量合并到现有 `DeliveryQualityInput`，真实超峰值文件不会被标记为成功。

## 仍未宣称完成的边界

本批已完成：可选 LUFS 目标、显式静音策略、字幕阅读速度、最终渲染字幕 bbox、Qt/PIL 定位一致性、title/action/platform/custom 安全区、品牌元素实测记录合同、EditorialBrief 持久化、声明式素材标注到时间线的 source→timeline 物化、平台封装/码率/文件大小/画幅合同、20 秒长样本/29.72 秒全源长度四编码、双 worker 以及双源四任务并发实测。仍未完成：对任意未标注素材执行视觉 Logo/免责声明自动识别、更大规模多源长片持续并发和完整商业端到端验收；当前内置 profile 是项目默认合同，不代替供应商最新政策。P2 真实 provider 凭据激活与 P11 真实灰度运营数据仍需相应外部环境。
