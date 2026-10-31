# bench_out/ 第 3 组目录条目卡（计划05 P0 基准分析）

覆盖：m8、m9、m10_encode_probe、m103_slice_rows、m104_pipeline、m1063_kernel、m11_stage1、m11_stage2b、m11_stage4、macos_gpu_export_20260915、metal_timeline_export、ai_upgrade，共 12 个。

通用背景：本组全部目录均为 HISTORICAL.md 标注的**历史观察**（各期 dirty 工作树、无统一 manifest），不得作为"当前性能/质量"结论引用；相对结论只有在同 clean HEAD、同 dylib SHA、交替 A/B 中位数 + bootstrap 95% CI 且区间不跨 1.0 时才可写（HISTORICAL.md 规则 2）。m8/m9/m10*/m11* 系列机器口径混杂（详见各卡），绝对 fps 跨目录不可比。

## m8
- 时间：约 2026-08-31（对应 M8 总验收 commit c8f4e915「V1 全矩阵公平对照实测 + ADR-C026 冻结」；JSON 内无时间戳）
- 测试对象：M8 codec 全矩阵验收——topos V1 vs ProRes HQ vs DNxHR HQX，2K/4K 四类口径：冷启动打开、解码（顺序/随机+RSS）、编码生成（QP 定标 20–60）、画质（PSNR/SSIM/多代重编码）
- 关键指标（出处：m8/*.json，均为结构化 JSON）：
  - 4K 冷启动中位：topos 281.6 ms vs prores 196.1 / dnxhr 181.4（cold_4k.json）；1080p：263.6 vs 123.7/120.1（cold_1080p.json）
  - 4K 顺序解码 avg：topos 12.30 ms vs prores 8.69 / dnxhr 5.97；随机解码 avg：topos 12.72 ms vs prores 73.15 / dnxhr 51.88——随机 seek 快 ~5–6×（decode_4k.json）
  - 4K RSS max：topos 186.4 MB vs prores 582.6 / dnxhr 952.7，内存占用 ~3–5× 优势（decode_4k.json）
  - 4K 编码 avg：topos(qp57) 82.77 ms vs prores 240.9 / dnxhr 30.8；码率 612.83 Mbps（vs prores −5.1%）；但 topos_sized 目标码率模式 188.2 ms、rate_error −20.54%（gen_4k.json）
  - 4K 画质：PSNR-Y topos 60.39 vs prores 65.58 / dnxhr 64.07 dB；SSIM-Y 0.99903 vs 0.99972/0.99963；多代重编码 gen10 时 topos 59.65 dB 仍高于 prores 57.15（世代衰减更缓，quality_4k.json）
  - 线程一致性：t1/t2/t4/t8 packet_bytes 完全一致（4K 21,673,891 B），encode/decode_match 全 true（gen_*.json thread_parity）
- 产物类型：json
- 主题分组建议：codec（vs 参照 codec 全矩阵）+ 画质（PSNR/SSIM/多代）子域
- 可比性备注：dirty 工作树历史口径，机器/构建未记录于 JSON 内；sized 码率误差 −18~−21% 与 m11_stage1（±0.5%）不可直接对比（RC 已重写）；本目录编码产物 .mov 已不在（gen_*.json 引用的 path 为当时路径）。

## m9
- 时间：2026-08-31（lib_commit=93e72e84「M9 §12.5 V2 门禁覆盖」，2026-08-31）
- 测试对象：M9 V2 VLC 码流验收（v1 vs v2 全链路对照）——2K/4K × qp 40/50/57 共 6 格，码率/编解码耗时/重建一致性
- 关键指标（出处：m9/vlc_acceptance.json）：
  - 码率：v2 相对 v1 全部 6 格 −27.9% ~ −31.7%（rate_delta_pct），4K qp50 最大 −31.65%
  - 解码提速：decode_speedup 1.00–1.17×（4K qp50 达 1.17），1080p 三格 ~1.0
  - 编码耗时：v1/v2 p50 基本打平（如 1080p qp40：27.57 vs 27.79 ms）
  - 重建一致性：recon_parity 全 6 格 true
  - 环境：threads=8、48 帧、每格 3 次重复
- 产物类型：json
- 主题分组建议：codec（V1→V2 熵编码演进）｜AB演进
- 可比性备注：v1/v2 以同 lib_commit 内分流实现（非两库交错），无 CI；−28~−32% 码率结论与 5bc83bef RD 曲线（V2@qp45 2K 省 29.5%）方向一致，可互为佐证；commit 5ce3b40a「M9 验收：码率 −28~32% 锁定」即引用本口径。

## m10_encode_probe
- 时间：2026-09-02（文件名 20260902_*；Mac-Pro i9-9900K 8P/16L）
- 测试对象：M10 编码路径剖析与探测优化——产品口径分段计时（python输入/native/mux、fill/entropy/probe量化/CRC）、同线程解码对照（prores/dnxhr SLICE/FRAME/auto 线程型）、m10-6.1 持久缓存 A/B、m10-6.2 全 plane 统一提交 A/B、m10-7c strict 档真实素材验收
- 关键指标（出处：目录内 txt/json）：
  - 2K qp20 t8：wall 40.6 ms（24.66 fps），CPU 占比 entropy 62% / fill 32% / crc 4%（20260902_2k_qp20_t8.txt）
  - 4K sized t8：wall 195.6 ms（5.11 fps），probe量化占 CPU 82%，probe 编码 10 次/帧（20260902_4k_rate_t8.txt）
  - 同线程解码 t8 中位：2K topos 332.2 vs prores-slice 418.2 / dnxhr-slice 547.6 fps；4K topos 89.8 vs prores-slice 90.8 / dnxhr-slice 139.2（decode_same_threads_*.json）
  - m1061 持久缓存：4K sized 126–129 → 108 ms（−16%，fps 7.8→9.2）、2K −17~20%；稳态零分配断言 + 开/关逐字节一致（m1061_cache/20260902_ab.txt）
  - m1062 全 plane 统一提交：4K −11%（255→228 ms），1080p 打平（m1062_ab/summary.txt）
  - m107c strict 真实素材：2K 30.47 fps（probe 4 次、qp63 钉死、32/32 帧超 target +15.16%）；4K 6.82 fps（probe 7 次、误差中位 −4.77%）；对照改动前 0f404603：2K 18.40 / 4K 5.26 fps（m107c_strict/20260902_real_material.txt、20260902_batch_summary.txt）
- 产物类型：txt（人工摘要）+ json（结构化）
- 主题分组建议：codec（编码内核/率控探测演进）｜AB演进
- 可比性备注：A/B 均为同机同窗交错（TOPOS_CODEC_LIB 双库），但工作树 dirty、无 manifest 锚定；「2K topos 332 fps」解码口径与 m1063 终局（332）同源可衔接；probe 次数/qp 钉死等 strict 语义仅对该合成素材成立。

## m103_slice_rows
- 时间：2026-09-01（目录名 20260901_*；topos_commit=0b6322d8，2026-09-01「4K 编码线程扩展扫描」）
- 测试对象：slice_rows 粒度扫描（32/16/8）× t8/t16 编码+解码，2K/4K rate 模式 hq 档 24 帧——M10-3 默认值改 16 的前置证据
- 关键指标（出处：m103_slice_rows/*/result.json）：
  - 2K 编码 t8：sr32 14.42 → sr16 16.21 → sr8 17.91 fps（+12%/+24%）；解码 t8：sr32 193.9 → sr16 279.6 → sr8 262.7 fps（+44% @sr16）
  - 4K 编码 t8：sr32 4.30 → sr16 4.59 → sr8 5.00 fps（提升远小于 2K）；解码 72.7 → 76.5/76.8（+5%）
  - 码流体积随粒度变化极小：bytes_delta_vs_base ≤ 0.4%（4K ≤0.088%）
  - t16 与 t8 差异在噪声级（4K sr32 编码 4.30 vs 4.38 fps）
  - 环境：cpu="Genuine Intel 0000"、logical_cpus=24（非 Mac-Pro 主力机）
- 产物类型：json
- 主题分组建议：codec（slice 粒度/并行调度）
- 可比性备注：机器为 24 逻辑核 Intel（与 m10/m11 系列的 8P/16L Mac-Pro 不同），绝对 fps 不可与其它目录比；commit 0b6322d8 在 m1063 的 slice_rows 证据链中被引用（线程扩展饱和证据），两者互补。

## m104_pipeline
- 时间：2026-09-02（目录名 20260902_*；topos_commit=e291c87b，2026-09-01「M10-3 phase1 编码器默认 slice_rows 32→16」）
- 测试对象：编码流水线化（serial vs pipeline worker2/worker3）2K/4K × t8/t16，48 帧，bitexact 校验——共 6 次运行（含两轮迭代）
- 关键指标（出处：m104_pipeline/*/result.json）：
  - 前两轮（000717/004550/004944/004958）：2K serial 63.4–63.7 → w2 71.3–72.0 fps（+13%）；4K serial 17.7–19.2 → w2/w3 18.2–19.4（基本打平）
  - 后两轮（010250/010301，代码再迭代后）：2K serial 114.0 → w3 175.5 fps（+54%）；4K serial 31.3 → w2 38.1 fps（+22%）
  - bitexact_vs_serial 全部 true（流水线不改变位流）
  - 同目录同 commit 下前后两轮 fps 差近 2×（63→114），说明窗口内代码仍在快速演进，目录内部即非单一可比状态
- 产物类型：json
- 主题分组建议：codec（编码流水线）｜native域与线程
- 可比性备注：同 m103 的 24 核 Intel 机器；6 个 result.json 仅靠时间戳区分迭代轮次、无版本说明，引用时必须点名具体子目录；与 m11_stage1 stage3b（产品级渲染∥编码 +44%）口径不同（本目录为 codec 内流水线）。

## m1063_kernel
- 时间：2026-09-02（Mac-Pro i9-9900K 8P/16L；四项优化 commits：e919d9c5/2eb2b720/83188271/4a60c640，均 2026-09-02）
- 测试对象：M10-6.3 内核提速批次总验收——A 跨帧 qp 提示 / B AVX2 量化+掩码跳扫 / C 熵融合发射 / D 解码扫描微优化，四项各自 A/B + 输出不变性差分；附 6 份 sample 函数级剖析（profile_*.txt）与 M10-6.4 默认 slice_rows 32→16 证据
- 关键指标（出处：目录内 txt）：
  - 编码 sized：4K 4.70 → 12.60 fps（t16 13.69）；2K 18.65 → 40.61（20260902_batch_summary.txt）
  - 解码 t8：4K 72.9 → 89.8 fps（+23%）；2K 278 → 332（+19%）；其中 D 项 4K qp55 70.90→88.01（+24%，20260902_decode_ab.txt）
  - A 项 qp 提示：probe 次数 7→3（4K fps 8.35→12.91，+55%），bytes/target 误差分布不变（20260902_qp_hint_ab.txt）
  - 终局对照（同机同窗，FFmpeg 8.1）：4K 编码 topos hq 12.60 > ProRes 10.68（+18%）反超；2K 40.61 vs ProRes 43.17（−6%）；解码 4K 89.8 ≈ ProRes slice 90.8 打平，DNxHR 139.2 仍领先（batch_summary）
  - 画质钉死：PSNR 与优化前逐位一致（62.84/67.98 dB）——全部为纯提速（batch_summary）
  - slice_rows 默认 32→16 证据：2K 解码 259.15 → 382.25 fps（+48%）；全代价矩阵 2K sr16 enc +21%/dec +32%、PSNR −0.2 dB@67.7 档；最终同窗对照 2K enc 42.49 ≈ ProRes 42.94、4K enc 13.11 vs 10.51（+25%）（20260902_slice_rows_default_evidence.txt，原始数据引 bench_out/topos_vs_ref/）
- 产物类型：txt（摘要/证据）+ txt（sample 剖析大文件，6 份 145–163 KB）
- 主题分组建议：codec（内核优化批次）｜AB演进
- 可比性备注：本目录是 m8→m11 codec 性能叙事的枢纽：其"解码 332/89.8"与 m10_encode_probe 同口径衔接，其"产品路径 24→36+fps"继续在 m11_stage2b/3a/3c 兑现；A/B 均同窗交错 + 差分钉死输出不变，方法学在该时期最严谨，但仍为 dirty 树无 manifest；参照对比的 FFmpeg 版本 8.1 已注明。

## m11_stage1
- 时间：2026-09-02（i9-9900K；含 stage1 RC、stage2a、stage3a、stage3b 四份验收 + 1 份单遍剖析）
- 测试对象：M11 导出提速阶段 1（帧间反馈单遍码控 ToposRateFeedback）、2a（编码平面免拷贝直通）、3a（Python 绑定零拷贝平面传递）、3b（导出渲染∥编码流水线化）——stash 同窗 A/B + sha256 位精确验证
- 关键指标（出处：m11_stage1/*.txt）：
  - 阶段1 RC 单遍：4K 产品 t16 15.18 → 19.64 fps（+29%）、用户素材 15.93 → 19.79（+24%）；码率从系统性 −4.9% 欠靶修到 ±0.5%；PSNR 同码率 +0.3~0.55 dB；内核口径 sized 32.90 → 59.72 fps（1.81×）（20260902_stage1_rc_singlepass.txt）
  - 阶段2a 平面直通：内核 4K qp55 t16 53.91 → 71.82 fps（+33%），产品 +8%；累计 15.93 → 24.9 fps = +56%（20260902_stage2a_plane_direct.txt）
  - 阶段3a 零拷贝：产品 21.2 → 23.1 fps（+9%），10 帧输出 sha256 完全一致（20260902_stage3a_zerocopy.txt）
  - 阶段3b 流水线：真实负载全管线 1.54 → 2.22 fps（+44%，480MB 输出一致）（20260902_stage3b_pipeline.txt）
  - 剖析依据：tc_plane_pad_u16 占编码线程 ~29%（1017/3478），为 2a 动机（profile_enc_4k_qp55_singlepass.txt，153 KB）
- 产物类型：txt（验收报告）+ txt（剖析）
- 主题分组建议：编码导出（导出管线提速）+ codec（率控/内核）
- 可比性备注：各阶段均 stash A/B 同窗、位精确（sha256）验证，可信度较高但 dirty 树；注意口径词"产品路径/内核/机制"三级差异巨大（19.79 vs 59.72 fps 同为 4K），引用必须注明口径；m8 的 sized −20% 码率误差与本目录 ±0.5% 属不同代 RC，不可混用。

## m11_stage2b
- 时间：2026-09-02 晚（i9-9900K 8C16T；含 2b-A/2b-B/C028/3a-mux-CRC/3c 五份记录）
- 测试对象：M11 阶段 2b 与衍生——两组按门禁（wall<3% 回退）**主动回退**的负结果（融合 AVX2 前向+量化内核、逐 slice CRC 并行化、CRC 折进组装拷贝 ADR-C028），加两项落地正收益（mux 死 CRC 摘除 +6.6%、绑定层精确拷贝 +48~60%）
- 关键指标（出处：m11_stage2b/*.txt）：
  - 2b-A 融合内核：baseline 28.31/29.19 vs fused 27.82/28.20 fps（噪声级持平）→ 回退（20260902_stage2b_kernel_crc_attempts.txt）
  - 2b-B CRC 并行：微任务 39.4/42.2 vs 串行 44.0 fps（净亏）——唤醒风暴成本 > 2.24ms 串行 CRC 本体 → 回退（同上）
  - C028 CRC 折叠：表驱动 +2.8%、PCLMULQDQ **−7.5%**（10/10 方向一致）未达 ≥3% 门 → 整案回退；t1 判别实验发现 check 在 t1 仅 3.88ms vs t16 19.2ms（缓存态强依赖），2b-B 的"省 ~1ms"外推前提不成立（20260902_c028_crc_fold_reverted.txt）
  - mux 死 CRC 摘除：成对比值中位 1.066（+6.6%，10/10 对全正 [1.019~1.083]），输出 sha256 不变（20260902_stage3a_mux_dead_crc.txt）
  - 绑定层 .raw 精确拷贝修复：绑定口径 28.73 → 44.73 fps（+56%）、产品 24.16 → 35.6–40.6（+48~60%），码率贴靶不变（20260902_stage3c_binding_exact_copy.txt）
  - 附录：编码重载荷下 t16/t8 = 0.799（t16 慢 20%），产品 min(8,cpu) 线程策略依据（mux_dead_crc 附录）
- 产物类型：txt（含 ADR 实施与回退记录）
- 主题分组建议：codec（负结果/回退档案）｜AB演进｜native域与线程（t8/t16 结论）
- 可比性备注：负结果卡的价值在方法论（门禁、成对 A/B、t1 判别实验拆缓存伪象）；C028 记录的三轮作废基准事故（标签映射错位、计数单位错、payload 校准）是引用历史数据时的经典陷阱清单；+6.6%/+56% 两项正收益已被后续代码吸收，勿再当作可复得收益。

## m11_stage4
- 时间：2026-09-02（i9-9900K 8C16T）
- 测试对象：M11 阶段 4——解码侧跨帧任务图（tc_frame_decode_batch/tc_decoder_decode_batch 单批跨帧切片队列），成对差分 A/B（20 对，sha256 逐平面对拍），产品形态 vs 纯内核双口径
- 关键指标（出处：m11_stage4/20260902_stage4_decode_batch.txt）：
  - 2K hq rate 纯内核：390.6 → 429.4 fps（+10.4% [+8.0,+14.6]），越过阶段 4 验收线 420；对 ProRes 同窗 448.2 从 76% 追至 96%
  - 2K 产品形态：343.1 → 370.0 fps（+5.5% [+3.0,+9.6]，20 对 18 正）
  - 4K 纯内核：98.5 → 103.4 fps（+5.2%）；产品形态 83.8 → 81.8（−2.9%，包物化拷贝为基准形态人为成本，产品预取路径无此项）
  - t16：seq 300.3 → batch 362.3（+20.6%）但仍不及 t8+batch（379）→ t8 维持最优工作点
  - 批大小：2K 最优 8–9；4K 大批转负，建议 ≤4–6
- 产物类型：txt（验收报告）
- 主题分组建议：codec（解码批量化）｜播放（解码吞吐）
- 可比性备注：成对差分（同进程相邻成对、逐对比值中位数）是 m11 系列里最接近 HISTORICAL.md 规则 2 的方法，但无 bootstrap CI 且 dirty 树；「产品形态 −2.9%」的负项作者已归因为基准形态 artifact，引用时须带此注；正式复现脚本未入库（临时 /tmp 脚本）。

## macos_gpu_export_20260915
- 时间：2026-09-15T08:39Z（manifest created_at）
- 测试对象：macOS GPU（VideoToolbox）导出benchmark的产物——1920×1080@25fps、480 帧 H.264 导出（源 tests/test_videos/2K_h264.mp4），带完整 topos.export_manifest.v1
- 关键指标（出处：macos_gpu_export_20260915/2K_h264_480frames.mp4.manifest.json）：
  - 输出：39,866,423 B（约 6.64 Mbps @25fps/480 帧），sha256 eedcab0c… 已锚定
  - 配置：ExportCodec.H264、1920×1080、25fps、无音频/字幕、single_clip、ENTIRE 范围
  - delivery_qc：passed=true，blocking/warning/info 全 0
  - preflight：ok=true；capabilities registry 12 项（含 fcpxml/otio partial、bwf available 等）
  - manifest 内**无任何计时/性能字段**（timing 需另查同日 bench_out 根的 cpu_real.mov / gpu_real.mov 对照产物）
- 产物类型：mp4 产物 + manifest json（非指标数据）
- 主题分组建议：编码导出（VT/GPU 导出产物锚点）｜杂项
- 可比性备注：这是"带 manifest 的导出产物"而非基准结果，唯一价值是输出字节级锚点与当日 GPU VT 链路可跑通的证明；性能结论须去 e1_encode/d1_playback 或根目录 cpu_real/gpu_real 的 manifest 对照，本目录内无数据支撑 fps 声明。

## metal_timeline_export
- 时间：2026-09-15（脚本 mtime；__pycache__ cpython-312/313）
- 测试对象：真实素材时间线导出基准 **harness**（run_real_h264_ab.py，29 KB 可执行脚本）——Metal/OpenGL 后端 × h264/topos_proxy codec × sequential/stack 模式 × 16 种混合模式 × 9 类效果（blur/sharpen/glow/erode_dilate/edge_detect/denoise/defocus/chain），支持 --cpu-compositor 对照 CPU 合成路径
- 关键指标（出处：metal_timeline_export/run_real_h264_ab.py）：
  - 环境变量协议：TOPOS_EXPORT_BENCH_FRAMES（默认 120）/WIDTH（1280）/HEIGHT（720）/MEDIA/SECOND_MEDIA/OUTPUT_DIR（默认 /tmp/topos_metal_timeline_bench）
  - 输出：每臂写 result JSON（fps 等 metrics + metal_fallback_reasons + output_path）
  - 明确标注口径：GPU 渲染结果回读后交 FFmpeg/VT 编码，"不是 encoder-native 零拷贝 surface 路径"（脚本内注释）
  - topos_proxy 输出走应用内 MediaSourceFactory probe（FFmpeg 无 Topos 解码器注册）
  - 本目录内**无任何运行结果**（仅脚本 + pycache）
- 产物类型：py（benchmark 脚本/harness）
- 主题分组建议：编码导出（GPU 时间线导出基准工具）｜native域与线程
- 可比性备注：纯工具卡——ai_upgrade/current_anchor 与 T-P1-21 的 child_timeline_*_forced_gpu 数据即由该族脚本（或其变体）产生；引用性能数字请去那些目录，本目录只提供口径定义（默认 1280×720@120 帧与多数结果卡的 1920×1080@48 帧不同，须核对当次参数）。

## ai_upgrade
- 时间：2026-09-17 至 2026-09-19（47 个子目录 + 1 份根级矩阵日志 ai_upgrade_T-P1-60_matrix.log）
- 测试对象：docs/ai_executable_optimization_upgrade_plan_2026-09-17.md 计划的任务级证据树——T-P0-xx（能力快照/VT bridge/Metal surface）、T-P1-xx（直通快路径/CPU 编码优化/效果契约/GPU 效果链/Composite 节点矩阵/Deliver 契约/稳定性）、W01–W08 真实工作流回放矩阵、A2 回归复验；机器：macOS 15.3.1 x86_64 + AMD RX 6800 24 核（X-TEST-00 环境 manifest）
- 关键指标（出处：ai_upgrade/*/result.md 及 json）：
  - T-P1-10 TOPOS identity 直通：identity→topos_proxy 10.15 → 80.05 fps（7.9×），达 codec-only 90.22 的 88.7%（T-P1-10_20260917-232524/result.md）
  - T-P1-11 默认 slice_rows 16→8：2K t16 编码 121.80 → 170.28 fps（+39.9%）、2K 解码 204.8 → 294.5（+43.8%）；锚点 codec-only 90.22 → 101.42（+12.4%）、identity 80.05 → 95.02（T-P1-11_20260918-061227/result.md + encode_stage_matrix.json 36 组合）
  - T-P1-21 GPU 效果链解锁：grade 1.38 → 33.47 fps（24.3×）、blur 7.1×、glow 9.2×、two_layer 12.0×（基线 forced_gpu 与 cpu 同速=GPU 从未生效的结构性缺陷修复）（T-P1-21_20260918-085120/result.md）
  - T-P0-31 批次二全 GPU 链：240 帧 1080p H.264 暖进程 93.26 fps（≥60 达标）、冷 49.69；阶段 p50 decode 2.9 + render 9.2 + encode_submit 0.18 ms；full_gpu_blockers 首次为空（T-P0-31_20260919-015545/result.md + perf.json）
  - current_anchor 全链路锚点：原生解码 415.06 / 编码 159.33 / 解码→编码 101.45 fps；timeline identity cpu 91.4 / forced_gpu 83.6；grade cpu 1.60 vs gpu 36.62 fps（current_anchor_20260918/topos_anchor_matrix.md）
  - 稳定性：T-P1-71 H265 200 轮长跑 slope 2.199 MB/轮 < 5 阈值判"有界"；93 节点矩阵 77 passed_nonflat / 0 不可解释（T-P1-71_20260919-110914、T-P1-60）；W01–W08 8/8 PASS，A2 回归（缓存格式 gbrap12a16 变更后）零回归（W_matrix_20260919-141056、A2_regression_20260919-141050）
- 产物类型：md（任务验收报告）+ json（矩阵/锚点/manifest）+ 少量 mp4 产物与 manifest（T-P1-71 内存轮、T-P0-31 输出）
- 主题分组建议：AB演进（计划任务证据树，主分组）+ 编码导出（导出管线/GPU 链）+ native域与线程（VT/Metal/无头 GL 诚实标注体系）
- 可比性备注：① 全目录贯穿 X-TEST-10 诚实标注规则（无头 GL fallback ≠ 桌面 Metal、缺测量标 None 不标 0），质量高于一般历史数据，但仍是 dirty 树（分支 chore/edit，无 P0-04 manifest 树）；② codec fps（101–170）与 m10/m11 系列（12–60）协议完全不同（时间线导出 48 帧 vs 内核循环），绝对值互比无意义，只能比各自目录内的 A/B 比值；③ 多个 T-P0-31_*_0141-0155 小目录为脚本迭代中间态（仅个别有 result.md），引用以 T-P0-31_20260919-015545 为准；④ 与 macos_gpu_export_20260915 同周同机器群，GPU 导出叙事可衔接。
