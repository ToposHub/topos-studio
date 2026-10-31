# bench_out 第 4 组数据卡片（P0 基准分析，2026-09-28）

覆盖目录（11 个）：p0_runner、p2_09、qgain、readahead、real_2k_h264_20260907、real_codec_bitrate_20260907、real_user_workflow_h264、real_user_workflow_h264_full、reexport、round2、topos_anchor_matrix_20260917。
基准纪律依据 `bench_out/HISTORICAL.md`（P2-08）：历史目录一律"历史观察"，相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 且不跨 1.0。本组仅 readahead 部分结论接近该门槛，其余引用时须带可比性备注。

---

## p0_runner

- **时间**：2026-09-16 00:48–00:58（+0800），3 次连续运行。
- **测试对象**：P0 播放基准 runner（`p0_benchmark_runner`）对产品媒体源的分阶段解码性能：run1 为跨 codec 混合矩阵（commit `7b13746e`，B-01~B-04 场景，7 个素材），run2/run3 为 TOPOS 2K/4K 全档位扫描（commit `df8e2847`）。协议：warmup 10 / steady 120 / seek 30 / longrun 600，`repeats=1`。
- **关键指标**（出处：p0_runner_20260916_005041 / _005645 / _005815.json）：
  1. 稳态顺序解码 p50：2K h264 2.234 ms（447.6 fps）、4K hevc 22.451 ms（44.5 fps）、4K prores422HQ 31.175 ms、4K DNxHR HQX 25.759 ms、4K topos 13.227 ms、4K topos_proxy 4.715 ms（212.1 fps）［005041 steady_sequential］。
  2. 随机 seek p50：4K hevc 376.1 ms（max 625 ms）vs 4K topos 12.9 ms、topos_proxy 4.6 ms——TOPOS 随机访问优势约 29–80×［005041 seek_random］。
  3. TOPOS 2K 档位稳态 p50：proxy 1.351 ms → standard 2.612 → lt 2.094 → hq 3.728 → 4444 5.738 → 4444xq 7.031 ms［005645］。
  4. TOPOS 4K 档位稳态 p50：proxy 4.637 ms → 4444xq 22.465 ms［005815］。
  5. longrun 600 帧 RSS 增量全部 ≤ 13.4 MB（多数 < 4 MB，DNxHR 为 0），无泄漏迹象［005041/005815 longrun.rss_delta_bytes］。
- **产物类型**：JSON 结果（含 machine/git_commit 元数据，无独立 manifest）。
- **主题分组建议**：播放（跨 codec 播放性能矩阵；TOPOS 档位部分亦可挂 codec）。
- **可比性备注**：`repeats=1` 单次无 CI，不满足 HISTORICAL.md 相对结论门槛；run1 与 run2/3 的 git commit 不同（7b13746e vs df8e2847）；绝对数字引用须注明构建与协议（p50/p95、帧数口径）。冷启动 cold_first_frame n=3 样本极小。

## p2_09

- **时间**：2026-09-16 11:47–11:48，两次探针。
- **测试对象**：P2-09 媒体源会话池（LRU cap=12 + lease）多流打开/轮换行为：4K h265 与 4K prores 的单流吞吐 vs 16 流轮换后的热流/冷首帧/逐出重建。
- **关键指标**（出处：p2_09_pool_20260916_114756 / _114837.json）：
  1. h265_4k 单流：39.9 fps（p50 8.52 ms，p95 29.6 ms），frame_type=vt_pixel_buffer［114756］。
  2. 16 流轮换：streams_opened=16、cap=12、evictions=4、live_after_rotation=12，热流 p50 0.15 ms / 128.3 fps，冷首帧 p50 35.2 ms，evicted_stream_rebuild_ok=true［114756 h265_4k:rotation_16_streams］。
  3. prores_4k 单流 44.5 fps（p50 15.86 ms）；轮换后热流 46.9 fps、冷首帧 p50 165.3 ms——ProRes 冷重建比 h265 贵约 4.7×［114756］。
  4. 第二次运行剔除 2 帧预热后：h265 单流 107.2 fps（p50 4.23 ms）、prores 63.0 fps（p50 16.15 ms）［114837，含 warmup_excluded_frames=2］。
- **产物类型**：JSON 探针结果（note 注明 probe 脚本在 commit message 中）。
- **主题分组建议**：播放（多流会话池容量/逐出策略）。
- **可比性备注**：两次运行口径不同（是否剔除预热帧），单流 fps 相差 2.7×，采信含 `warmup_excluded_frames` 的 114837；无 commit/dylib SHA/manifest 锚定，只能作行为验证与量级参考，不可作 A/B 相对结论。

## qgain

- **时间**：2026-09-21 16:12–16:20（report generated_at；deblock_cmp 系列同批）。
- **测试对象**：TOPOS codec 画质/量化预算调优：proxy 档码率预算（target_bpp patch）、去块（deblock）滤波开关 A/B、感知质量指标体系（block_h/v、grid、stair、sharp、edge PSNR）与 ProRes 同帧对照。母版 4K_prores4444.mov reformat 到 2K YUV 4:2:2 10-bit limited 作参考。
- **关键指标**（出处：base/budget/nodeblock/smoke_2K_report.json、deblock_cmp_*、toos_deblock_cmp_*）：
  1. base 2K proxy（target_bpp 0.64483）：33.35 Mbps，yPSNR 40.13 / edge 36.37 dB，sharp 0.838，grid 1.266，rgb_psnr 36.97；ProRes Proxy 同帧 39.862 dB / sharp 1.398 / rgb 36.68——TOPOS PSNR 略高但高频能量（sharp）明显低［base_2K_report.json］。
  2. 码率预算 patch（proxy.target_bpp 0.64483→0.7448，+15.5% 码率）：yPSNR +0.45 dB、edge +0.59 dB、rgb +0.44 dB，grid/stair 基本不动［budget_2K_report.json］。
  3. deblock 开启（2K proxy）：block_h 2.67→−0.25、block_v 3.23→−0.76（块效应基本消除），yPSNR +0.09 dB，dec_fps 365.2→344.2（−5.7%）［deblock_cmp_2K_topos_422_Proxy.json］；LT 档 block_v 2.16→−0.01［deblock_cmp_2K_topos_422_LT.json］。
  4. 4K 各档 deblock 后 dec_fps 变化 −4%~+43% 波动（如 LT 48.2→69.0、4444 46.7→33.8），非成对测量［deblock_cmp_4K_*］。
  5. nodeblock（无去块参照）：block_h 3.878 / block_v 4.37，dec_fps 602.1——量化块效应是 proxy 档主要画质损失来源［nodeblock_2K_report.json］。
  6. toos_deblock_cmp：2K 全档新旧库输出 y_sha 逐位相同；4K ultra/high 的 f1 像素差 40034/40438（f0 相同）［toos_deblock_cmp_2K/4K.json］。
- **产物类型**：JSON 报告 + PNG 帧提取（base/deblock/blockiness 各 3 通道）。
- **主题分组建议**：画质。
- **可比性备注**：deblock_cmp 的 "old" 库在 /tmp（topos_prefix_lib、topos_old），无 SHA 锚定且可能已失效；toos_deblock_cmp_2K 的 old_lib 与 new_lib 路径相同（对比无效）；4444_XQ 行 y_psnr=−1.02/sharp=26.8 是参考口径不含该档所致，不能当质量退化引用；均为单次测量无 CI。

## readahead

- **时间**：2026-09-02（M11 阶段4 系列四份证据文档，i9-9900K 8C16T）。
- **测试对象**：ToposMediaSource 产品路径解码 IO/线程优化的完整证据链：①顺序读前批量预取；②原生批量包聚读 arena（tc_movie_packet_batch，顺带修复 bytes use-after-free 隐患）；③parse 并行化候选否决；④t8/t16 默认线程静机复验。
- **关键指标**（出处：20260902_stage4_product_readahead.txt / _arena_batch_read.txt / _parse_parallel_negative.txt / _t16_quiet_reverify.txt）：
  1. 读前批量预取 A/B：2K 296.0→368.4 fps，+24.3% [+22.2, +28.4]，12/12 对为正；4K +2.3% [+0.6, +3.6]，9/12 正［product_readahead］。
  2. arena 聚读最终官方窗口（空机 12 对）：2K 324.0→362.4 fps +10.5% [+7.8, +15.8]；4K 86.6→98.3 fps +11.7% [+7.4, +14.9]，10/12 正；IO 层隔离 4K arena vs bytes 2.22×［arena_batch_read］。
  3. parse 成本 0.78 µs/包（2K hq）/1.26 µs（4K hq），占帧解码 0.033%；Amdahl 反推串行占比 s≈0.5–1.2% → 并行化否决（收益 <0.1%，低于 3% 回退线）［parse_parallel_negative］。
  4. t16/t8 静机交错成对（12 对/分辨率）：2K 比值中位 0.998（无差）、4K 1.020（t16 略优）；同时发现默认线程口径文档与代码三处分叉（实际默认已是 16）［t16_quiet_reverify 原始逐对日志］。
  5. 内存：2K 稳态 RSS 第 8 遍起平台化 ~195 MiB，12 遍无持续增长［product_readahead］。
- **产物类型**：方法学证据文档 txt（含机制、A/B 方法、门禁测试、复现命令、原始逐对数据）。
- **主题分组建议**：AB演进（native 域与线程优化可交叉引用）。
- **可比性备注**：本组内质量最高的相对结论来源——交替成对 + 比值中位数 + 区间，基本符合 HISTORICAL.md 精神；但仍无 manifest/commit/dylib SHA 锚定（历史观察）。注意文中自述：跨会话机器漂移 ±5%、一次与 ASan 并发窗口作废，只信同窗成对；t16 结论明确标注"静机"。

## real_2k_h264_20260907

- **时间**：2026-09-07 12:07（+0800，result.json generated_at）。
- **测试对象**：真实素材端到端转码基准：2K_h264.mp4（1920×1080/25 fps/1572 帧/H.264 High 4:2:0）以 proxy 档完整转 TOPOS，再全片解码 + 往返质量对拍。输入先 FFmpeg 解码转 yuv422p10le 后进产品 ToposVideoEncoder。
- **关键指标**（出处：report.md、result.json、quality.json）：
  1. H.264→TOPOS 端到端 16.362 s / 96.08 fps；TOPOS 编码净耗时 9.823 s / 160.04 fps［result.json encode］。
  2. TOPOS 全片解码 4.129 s / 380.69 fps（2.627 ms/帧）［result.json decode］。
  3. 120 帧口径解码对照：FFmpeg 1079.0 fps、VideoToolbox 304.0 fps、topos wrapper 135.6 fps（p50 6.12 ms）、topos native 335.6 fps（p50 2.68 ms）［result.json decode_comparison］。
  4. 输出 781,505,543 bytes / 99.43 Mbps，体积为 H.264 源的 14.88×（纯帧内 vs inter 编码的结构性差距）［result.json size_ratio］。
  5. 往返质量（对 H.264 解码后 4:2:2 10-bit，非原始源）：Y PSNR 54.201 dB、U 61.972、V 62.325，luma_max_abs 46，1572 帧全解［quality.json / result.json］。
- **产物类型**：report.md + result.json + quality.json（MOV 本体不在结果树）。
- **主题分组建议**：codec。
- **可比性备注**：使用工作区 Release dylib，无 commit/dylib SHA 记录；report.md 自我声明与 09-03 基准的趋势对比"不能当严格回归"；解码三方对照的像素格式/硬件路径不同，仅用于瓶颈定位。绝对值引用须注明构建。

## real_codec_bitrate_20260907

- **时间**：2026-09-07（与 real_2k 同日同机）。
- **测试对象**：TOPOS vs ProRes（FFmpeg prores_ks 对应 profile）各档码率对比，1920×1080/25 fps/96 帧；422 档用 2K_prores422HQ 解码帧，444 档用 4K_prores4444 Lanczos 下变换；同行 TOPOS 与 ProRes 使用同一组帧。
- **关键指标**（出处：report.md 表格、result.json tiers）：
  1. TOPOS/ProRes 体积比：Proxy 3.79×、LT 1.46×、Standard 1.52×、HQ 1.29×、4444 1.03×、4444 XQ 1.36×［result.json 各 tier mbps 比］。
  2. 绝对码率示例：HQ 档 TOPOS 136.84 Mbps vs ProRes 106.47 Mbps；XQ 档 584.61 vs 428.80 Mbps［result.json］。
  3. QP=63 地板超支：proxy 实际 0.9286 bpp vs 目标 0.6051；pro444 5.7631 vs 4.1106；extreme 11.2772 vs 6.7630——帧内 VLC 内核压缩能力上限，非配置误差［report.md 结论段 + result.json target_bpp］。
  4. 422 档 QP 实际区间：proxy 63–63、lt 60–63、standard 57–59、hq 54–56［result.json topos_qp_min/max］。
  5. pro444 行 ProRes 输出为 yuv444p12le（12-bit）vs TOPOS 10-bit，位深不对齐，非严格同质量比较［result.json prores_probe］。
- **产物类型**：report.md + result.json（MOV/raw 中间文件被 gitignore，不入库）。
- **主题分组建议**：codec（码率/压缩效率）。
- **可比性备注**：96 帧单次、无 CI；同一工作区 dylib 无 SHA；report.md 附完整复现命令（tools/bench_real_bitrates.py）。

## real_user_workflow_h264

- **时间**：2026-09-17 09:34（UTC，manifest created_at）。
- **测试对象**：真实用户工作流功能冒烟：2K_h264.mp4 导入 → demo 时间线（240 帧、1920×1080/25 fps）→ H.264 导出（single_clip、ENTIRE、烧录字幕、无音频）。
- **关键指标**（出处：demo_export_2K_h264.mp4.manifest.json）：
  1. 导出成功：H.264 yuv420p 1920×1080/25 fps/240 帧，输出 21,922,507 bytes，sha256=6b3b7cb7…3be09［manifest output 段］。
  2. delivery_qc passed：blocking/warning/info 全 0；preflight ok 无降级［manifest delivery_qc/preflight］。
  3. 素材锚定：2K_h264.mp4 sha256=3d420027…af04f（与 _full 工作流同源同素材）［manifest assets］。
  4. manifest 含 capability_registry 快照（edit.timecode.drop_frame、edit.interchange.bwf 等）——功能能力证据而非性能数据［manifest capabilities］。
- **产物类型**：导出交付物（demo_export_2K_h264.mp4 + demo_timeline_2K_h264.topos + .topos_cache）+ topos.export_manifest.v1。
- **主题分组建议**：编码导出（工作流冒烟/交付验证）。
- **可比性备注**：无任何性能计时字段，不可用于性能对比；价值在 manifest 完整性（素材 SHA、QC、能力注册表）与作为真实素材交付样本。

## real_user_workflow_h264_full

- **时间**：2026-09-17 09:38（UTC，manifest created_at）。
- **测试对象**：AI 全链路真实工作流验证：素材探测 → 建项/导入 H264（含音轨）→ 120 帧处刀切成两 clip → Gaussian Blur（r=8）效果 → 调色栈（exposure 0.2/contrast 0.12/saturation 0.16）→ Composite 节点图（4 节点 3 连接）→ 保存重载 → H.264 生产导出（含音频 aac 48 kHz）。
- **关键指标**（出处：workflow_result.json、ai_real_h264_workflow_export.mp4.manifest.json、startup_test.log）：
  1. 8 步全部 passed（all_passed=true），工程文件 5,593 bytes、重载后 1 asset/1 timeline/2 video clips/1 audio clip/1 effect/1 color_stack_ref 完整往返［workflow_result.json steps］。
  2. 生产导出：wall 9.4316 s / 25.446 fps，240 视频帧 + 451 音频帧，输出 22,105,859 bytes，probe 确认 h264 yuv420p/25 fps/aac［steps 末项 detail］。
  3. hw_fallback_reason 为空串（无硬件回退记录）、error_code 空［同上］。
  4. 启动测试：studio 产品模式、五大 feature 模块注册正常、exit_code=0；唯一 ERROR 为 ThumbEngine FFmpeg NOT found（缩略图非阻断）与 checkmark.svg 路径问题［startup_test.log / startup_exit_code.txt］。
  5. 导出 manifest（topos.export_manifest.v1）齐备，含两 clip 的 asset_usage 与 QC 通过记录［export manifest］。
- **产物类型**：工作流结果 JSON + 导出 mp4/manifest + .topos 工程 + 启动日志（完整功能证据包）。
- **主题分组建议**：编码导出（端到端工作流验证）。
- **可比性备注**：单次运行、无重复与 CI；25.4 fps 导出速率与 topos_anchor_matrix_20260917 的 identity 锚点（9.6 fps，输出 topos_proxy）方向参考须注明输出 codec 不同（H.264 vs TOPOS proxy）、不可直接比。

## reexport

- **时间**：无内部时间戳（文件 mtime 2026-09-22，疑为归档恢复产物）。
- **测试对象**：TOPOS 解码内核优化系列探针：线程扩展性（1/4/16）、解码滤波成本开关、branchless 优化 A/B、vectorized 探针、以及 2K/4K×7 档位 encode/decode 速度矩阵（含 ProRes 对照）。
- **关键指标**（出处：decode_threads_2k/4k.json、filtercost_on/off.json、branchless_3reps.json、reverted_3reps.json、vectorized_probe.json、spec_speed_matrix.json）：
  1. 线程扩展性（fps 中位，3 reps）：2K proxy 53.5/192.0/338.3（t1/t4/t16）；4K proxy 13.3/50.7/92.1；4K 422 全档 8.9/33.8/65.5；2K 422 33.1/115.4/227.6——16 线程约为单线程 6.4–7.4×［decode_threads_*］。
  2. 滤波成本：2K proxy t16 滤波关 537.2 fps vs 开 300.5 fps（滤波约占 44% 解码耗时）；4K 163.0 vs 84.7 fps［filtercost_off/on.json，2 reps］。
  3. branchless A/B：2K proxy t16 335.8（branchless）vs 296.1（reverted）fps（+13%），t1 49.7 vs 47.5；digest 两边相同（8ceb65fa705b5126）证明输出逐位一致［branchless_3reps vs reverted_3reps］；vectorized_probe t16 322.2 fps digest 一致［vectorized_probe.json］。
  4. 档位速度矩阵：2K netproxy enc 695.8 / dec 156.0 fps、hq enc 192.8 / dec 120.8、4444xq enc 101.1 / dec 142.2；4K netproxy enc 213.5 / dec 38.4、4444xq enc 33.3 / dec 49.3；bpp 命中率 ratio 0.985–1.003［spec_speed_matrix.json］。
  5. 滤波开启使 dec_fps 相对 no_filter 掉 3–4×（如 2K proxy 144.3 vs 545.9）；相对 ProRes 解码速度 2K 3.5×–14.1×、4K 4.7×–21.3×［spec_speed_matrix.json］。
- **产物类型**：JSON A/B 结果（3 reps 中位 + 输出 digest；filtercost 仅 2 reps）。
- **主题分组建议**：codec（速度矩阵）+ AB演进（branchless/filtercost/vectorized 探针）。
- **可比性备注**：全目录无 manifest/commit/dylib SHA/时间戳，跨文件 reps 口径不一（2 vs 3）；digest 一致性使性能差可作方向参考，但绝对值与 p0_runner/058d29da 树不可直接混用；"netproxy" 档为此处首见的扩展档位。

## round2

- **时间**：2026-09-01 05:15（52a2dbf0，dirty）；2026-09-03 13:00/13:03（d9050239，clean）。
- **测试对象**：TOPOS native 解码基准 round2（tools/bench_codec_round2.py）：1080p/4K，多层输入口径（A=packet 常驻内存直调；B/C=os.pread bytes→ctypes.memmove），200 帧 × 5–7 reps，cv_gate 3%，含早期 manifest（git commit + dirty + dylib SHA + 素材 SHA）。
- **关键指标**（出处：round2_invalidated.md、各子目录 decode_*.json / manifest.json）：
  1. 52a2dbf0 1080p A 层：avg 4.563 ms / 219.1 fps，cv 1.66% 过门；4K：12.893 ms / 77.6 fps，cv 3.37% 未过门（auto_reruns=2）［52a2dbf0_20260901_051525/decode_*.json——但见失效标记］。
  2. d9050239 1080p：4.866 ms / 205.4 fps，cv 0.76% 过门；4K：10.184 ms / 98.1 fps，cv 4.14% 未过门；13:03 复跑 4K：10.882 ms / 91.9 fps，cv 7.62% 未过门［d9050239_20260903_*/decode_*.json］。
  3. RSS 漂移均 ~0 MB；有效核数 4K 下 7.3→13.7（两次构建的线程行为不同）［decode_4k.json effective_cores］。
  4. 失效判定：52a2dbf0 的 `_Kernel._ensure_planes` 存在 use-after-free（P0-01），解码输出与全部统计不可信；2026-09-03 起修复（`self._plane_keep`）［round2_invalidated.md］。
- **产物类型**：JSON 分层基准结果 + manifest + 失效标记 md（证据链式保留）。
- **主题分组建议**：AB演进（历史基准迭代与失效治理案例）。
- **可比性备注**：**整目录按 round2_invalidated.md 与 HISTORICAL.md 处置**——52a2dbf0 数字全部禁用；d9050239 两跑虽在修复日当天之后，但其 library 路径指向另一工作树（/Users/testuser/Documents/vscode/topos/… 而非本仓），4K 全部未过 CV 门，且无与安全 decoder 的对拍记录，只可作历史观察；任何相对结论禁止引用本目录。

## topos_anchor_matrix_20260917

- **时间**：2026-09-17 19:38（topos_anchor_matrix.json generated_at）。
- **测试对象**：TOPOS 422Proxy 全链路速度锚点矩阵：native 解码 / 隔离编码 / 真实逐帧 decode→encode 三个 codec 锚点 + TimelineExporter 7 场景（identity/grade/blur/sharpen/glow/two_layer/transition）× 2 选路（cpu_render/forced_gpu）效果成本。源 2K_topos_proxy.mov，48 帧，warmup 8，输出 codec topos_proxy。
- **关键指标**（出处：topos_anchor_matrix.md / .json、child_decode_only / child_encode_only / child_decode_encode / child_timeline_*.json）：
  1. native 锚点：解码 433.92 fps（p50 2.16 ms）；隔离编码（重复单帧）147.17 fps（p50 6.48 ms）；真实逐帧 decode→encode 96.57 fps（wall 0.497 s/48 帧）［topos_anchor_matrix.md + child_*.json］。
  2. TimelineExporter identity 基线：cpu_render 9.598 fps、forced_gpu 9.775 fps——含调度/合成/编码/封装的生产型锚点，远低于 codec-only［child_timeline_identity_*.json］。
  3. 效果额外成本倍率（effect_total − identity 总耗时，再除 identity）：grade 6.02×、two_layer 3.23×、sharpen 2.38×、glow 2.37×、transition 2.22×、blur 1.73×；grade 额外成本高达 ~25.1 s（48 帧）［md 表格］。
  4. 所有 14 个 timeline 组合的 GPU trail/fallback_reason 均为 `cpu_encoder_surface_path`——即使 forced_gpu 请求，编码面仍 CPU，全链路 GPU 未达成（文档明确"不能算全链路 GPU"）［md 表格 + child JSON trail 字段］。
  5. forced_gpu 与 cpu_render 的 FPS 差异 < 3%（如 identity 9.60 vs 9.77），因路径相同，差异属噪声［md 表格］。
- **产物类型**：汇总 md + 主 JSON（156 KB）+ 16 个 child JSON（每场景/锚点一份，含 stage 级分段计时：entropy_ns/dequant_idct_ns/fill_ns 等）+ 1 个 .tmp 中断 manifest。
- **主题分组建议**：编码导出（效果链成本锚点；forced_gpu 路径证据亦可挂 native域与线程）。
- **可比性备注**：runs=1 单次无 CI，绝对值仅作锚点基线；child_timeline_glow_cpu_render_topos_proxy.mov.manifest.json.tmp 表明一次 manifest 写入中断，该 child 的交付侧证据不完整；native 库为工作区 build/release，无 SHA 锚定（历史观察口径）。
