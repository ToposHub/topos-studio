# bench_out 数据卡片 · 组 2（P0 基准分析,2026-09-28）

范围:`bench_out/` 第 2 组共 14 个目录。按 `bench_out/HISTORICAL.md`(P2-08)规则,
本组全部目录属于**历史观察**(非 `058d29da/` 不可变树),仅可作方向性参考;
相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 不跨 1.0 方可引用。
注:任务清单中 `cb4f3089` 为笔误,实际目录为 `cbf6b308`(已核对,无 `cb4f3089`)。
出处均为 `bench_out/` 下相对路径;`threads_eff` = manifest `threads_effective`。

---

## b0907366

- 时间:2026-09-07(20260907T033859Z / T034147Z)
- 测试对象:topos codec core/E2E 编码+解码 vs ffmpeg ProRes 422 HQ / DNxHR HQX,2K(8 帧,hq 档,clean)+ 4K(4 帧,hq 档,dirty)
- 关键指标:
  1. 2K core 编码 topos 0.138 s vs ProRes 0.315 s(ratio 0.439,CI95 [0.429, 0.448])、vs DNxHR 0.193([0.183, 0.203]),均 topos faster(`b0907366/63f18e17/8623acfd/.../result.json`)
  2. 2K E2E 编码 ratio 0.285(ProRes)/ 0.124(DNxHR);文件 5.50 MB vs 4.30/7.34 MB(同上)
  3. 2K 解码 ratio 0.318 / 0.299,topos faster(同上)
  4. 4K 轮次 `git_dirty: true` 且全部 ci95=null,interpretation=indistinguishable;E2E ratio 0.197/0.072 仅方向性(`.../a8cc28fd/.../result.json`)
  5. manifest:i9-9900K,slice_threads 16 但 threads_eff=4;abi 2
- 产物类型:manifest 化 JSON 结果(bench_manifest.v1,<commit>/<dylib8>/<manifest8>/<utc>/ 树)
- 主题分组建议:codec
- 可比性备注:2K 轮 clean 且 CI 合格可作 A/B 参考;4K 轮 dirty + 无 CI,不可引用相对结论;threads_eff=4 与同期 16 线程结果不可横比。

## c0f7068d

- 时间:2026-09-03(20260903T164719Z)
- 测试对象:topos 全档位自测矩阵(proxy/lt/standard/hq/pro444/extreme,2K+4K,422/44410/44412,96 帧),无参照编码器行
- 关键指标:
  1. 2K hq:190.9 Mbps、Y-PSNR 68.61、SSIM 0.99986、enc 17.5 ms/f、dec 6.3 ms/f(`c0f7068d/00be27d2/557a6269/.../codecs_matrix.md`)
  2. 4K hq:764.0 Mbps、63.41 dB、enc 54.7 ms/f、dec 15.5 ms/f(同上)
  3. 4K extreme(12-bit):1863.5 Mbps、67.96 dB、enc 85.3 ms/f(同上)
  4. 全部 Δ vs ProRes/DNxHR 为 n/a——参照缺位,同码率插值不可算(同上)
  5. manifest:clean,dylib 00be27d2,CPU "Genuine Intel 0000"(黑苹果),16 线程(codecs_matrix.json)
- 产物类型:manifest 化 JSON + 自动生成 MD 矩阵
- 主题分组建议:codec(偏速度自测)
- 可比性备注:仅 topos 单方数据,无 A/B;机器为黑苹果(CPUID 未知),绝对值仅供参考;与 cbf6b308(有参照)构成前后演进对照。

## c4082663

- 时间:2026-09-03(20260903T132512Z)
- 测试对象:topos RD 曲线 + 熵编码器 V1/V2 A/B,4K profile5/444/12-bit,48 帧,qp 25–60 扫描;另含 10 代重复编解码(generations)
- 关键指标:
  1. qp45 同画质(psnr_y 77.589)V2 码率 2591.96 Mbps vs V1 3368.80 Mbps,省约 23%(`c4082663/0df35db5/7973290a/.../rd_result.json` rows)
  2. qp60:V2 1285.5 vs V1 1760.6 Mbps(−27%),psnr_y 均 60.476(同上)
  3. qp≤30 无损:psnr=Inf、maxerr=0(同上)
  4. 多代:gen1→gen10 psnr_y 77.521→73.456,V1/V2 完全同质量轨迹(仅字节差异 16.74 vs 12.89 MB/f @gen10)(同上 generations)
  5. manifest:`c4082663-dirty`,dylib 0df35db5,黑苹果,16 线程
- 产物类型:manifest 化 JSON(RD+generations)
- 主题分组建议:codec|AB演进(V1→V2 熵编码)
- 可比性备注:dirty 工作树,且为同轮内 V1/V2 对照(非交替 A/B 中位数+CI),按 HISTORICAL 规则只能作方向性结论;无参照编码器。

## cbf6b308

- 时间:2026-09-03(20260903T152528Z)
- 测试对象:topos × ProRes × DNxHR 三方全矩阵(2K+4K,422 四档 + 44410 + 44412,96 帧,画质测前 96/48 帧)
- 关键指标:
  1. 2K 422 编码耗时 topos 0.136–0.238×ProRes(约 4–7× 快);解码 topos 2.0–2.6×ProRes(约 2× 慢)(`cbf6b308/e124e93c/90f1bf4f/.../codecs_matrix.md` 配对速度比)
  2. 同码率 Δ(负=topos 差):2K proxy −1.81 dB、LT −3.04 dB;4K proxy −2.07、LT −2.49、standard −3.86 dB(同上)
  3. 4K hq:enc 0.136×ProRes / 1.486×DNxHR,dec 0.998×ProRes / 1.141×DNxHR(同上)
  4. 4K 444:enc 0.135×ProRes / 0.983×DNxHR(同上)
  5. DNxHR 8-bit 参照单列,位深不同不进 Δ 比较(同上)
  6. manifest:clean,dylib e124e93c,黑苹果,16 线程(codecs_matrix.json)
- 产物类型:manifest 化 JSON + MD 矩阵
- 主题分组建议:codec
- 可比性备注:与 c0f7068d 同脚本不同 commit(clean),两组可看 dylib 演进;Δ 插值部分档位 n/a;黑苹果绝对值仅供参考。

## d9050239

- 时间:2026-09-03(20260903T044929Z)
- 测试对象:topos core/E2E 编解码 vs ProRes HQ / DNxHR HQX,2K+4K 各 96 帧 hq 档,交替中位数 + bootstrap CI
- 关键指标:
  1. 2K core 编码 vs ProRes ratio 0.984(CI [0.948, 1.002],indistinguishable);vs DNxHR 1.100([1.027, 1.141],ref faster)(`d9050239/c9e31afc/8c36c356/.../result.json`)
  2. 4K core 编码 vs ProRes 0.897([0.872, 0.903],topos faster);vs DNxHR 0.969([0.963, 1.002],indistinguishable)(同上)
  3. E2E:2K 0.575/0.617,4K 0.470/0.548,topos faster(同上)
  4. 文件大小:topos≈1.9× ProRes(2K 96.6 vs 51.1 MB;4K 386 vs 311 MB)(同上)
  5. 解码:4K ratio 0.764/0.746(topos faster);2K 两组均 indistinguishable(同上)
  6. manifest:clean,dylib c9e31afc,黑苹果,threads_eff 16
- 产物类型:manifest 化 JSON(core/E2E 双口径,含 CI)
- 主题分组建议:codec
- 可比性备注:本组内 CI 质量最高的对照之一(clean + CI),但机器为黑苹果;与 b0907366(i9-9900K, threads_eff 4)不可直接横比。

## d9552407

- 时间:2026-09-03(20260903T124654Z)
- 测试对象:topos vs ProRes RD 质量对照(码率/PSNR/SSIM,2K+4K,422 四档 + 444/444XQ,96 帧),无速度项
- 关键指标:
  1. 2K 同码率 Δ:proxy −1.809 dB、LT −3.042 dB;standard/HQ 插值越界 n/a(`d9552407/ce8ba9ab/c2bd6715/.../rd_vs_prores_result.json`)
  2. 4K Δ:proxy −2.068、LT −2.494、standard −3.863 dB(同上)
  3. 2K hq 档:topos 191.25 Mbps/65.46 dB vs ProRes HQ 106.47 Mbps/65.89 dB(同上 rows)
  4. 444 域 2K:topos 4444 305.15 Mbps/50.74 dB vs ProRes 4444 290.44/56.78 dB(差约 6 dB);topos extreme 12-bit 584.61/57.67(同上)
  5. manifest:clean,dylib ce8ba9ab,黑苹果
- 产物类型:manifest 化 JSON(质量矩阵)
- 主题分组建议:画质|codec
- 可比性备注:纯质量口径,与 cbf6b308(质量+速度)数据高度同源,引用时择一;Δ 为插值非 CI 化。

## codec_bench

- 时间:2026-09-03(report+tiers)、2026-09-16(christmas,M1)
- 测试对象:混合口径 harness:topos vs ffmpeg prores_ks/dnxhd(96 帧×3 次中位数)、六档位全量、以及 M1 MacBook Air 上 1080P 真实素材六档实测
- 关键指标:
  1. 4K hq 编码 sized:topos 45.2 ms/f(22.1 fps)vs ProRes 81.9 / DNxHR 73.1,领先 1.5–1.8×(`codec_bench/report.md` 结果表)
  2. 解码:2K topos 4.8 vs ProRes 4.0 ms(−16%);4K topos 13.2 vs 15.0/14.7(+12–14%)(同上)
  3. 六档 tiers:4K 全部六档 topos 编码领先 1.5–2.1×;解码 12 格中 11 格领先(同上附录)
  4. 运行间方差:2K hq 两轮 14.1 vs 23.0 ms(黑苹果调度波动),轮内背靠背相对结论稳定(同上)
  5. M1 Christmas:1080P 六档编码典型 42–171 fps 全部超 29.97 实时;4444XQ 363.7 Mbps(`codec_bench/christmas_1080p_results_20260916.md`)
  6. 含脚本 run_bench.py / run_bench_tiers.py / bench_christmas_1080p.py(同目录)
- 产物类型:MD 报告 + JSON + 基准脚本(唯一自带脚本的目录)
- 主题分组建议:codec
- 可比性备注:HISTORICAL.md 明确标为"dirty 工作树、混合口径 harness、历史观察(方向性参考)";三批数据机器/口径各不相同(黑苹果→M1),严禁互相比对绝对值。

## d1_playback

- 时间:2026-09-15(04:46–05:27 UTC)
- 测试对象:D1 播放边界 —— ToposMediaSource read_next_frame/read_frame → CPU planar 帧(排除 GPU 上传/调色/合成/呈现),顺序解码,3 个构建 × 4 种素材
- 关键指标:
  1. 2K proxy:~707 fps(p50 1.35 ms/帧)(`d1_playback/b5f4520a/fb0712a5/b98ca792/.../result.json`)
  2. 2K hq:249–277 fps(p50 3.5–3.7 ms)(`.../a0314c95/` 与 `b61cabf4/.../23a984a1/`)
  3. 4K proxy:187–204 fps;4K 4444xq:40–43 fps(p50 23–25 ms)(`.../db53552b/`、`.../8fedcb41/`)
  4. RSS 峰值:4K 4444xq 730–746 MiB,其余 83–146 MiB(各 result.json `rss_peak_mib`)
  5. 构建:1459cef4(dirty,16 帧预跑)→ b61cabf4 → b5f4520a(clean,96 帧×2 repeats);诊断字段 decode_path=context、bitstream_major=7
- 产物类型:manifest 化 JSON(bench_manifest.v1)
- 主题分组建议:播放
- 可比性备注:同素材跨 b61cabf4/b5f4520a 两 clean 构建结果差异 <10%(4K proxy 172→203 fps 属波动上界);无参照解码器,仅自身体量刻度;random_seeks=0 未测寻址。

## davinci_context

- 时间:2026-09-02
- 测试对象:达芬奇 Resolve 4K 导出差距归因上下文(用户实测 + 本机复现,i9-9900K;源 1080p H.264 → 2× 放大 → 10-bit 422 中间片,前 300 帧)
- 关键指标:
  1. 达芬奇用户实测:ProRes 422 导出 ~100 fps、DNxHR ~75 fps(`davinci_context/20260902_davinci_4k_export_context.txt`)
  2. 本机同任务 ffmpeg prores_ks ~14 fps;topos 编码内核(预放大)t16:proxy/standard/hq = 18.8/16.8/15.5 fps(同上)
  3. 整管线串行 10.5 fps → 生产者/消费者流水线化 13.4 fps(+28%);t16 比 t8 +8%(同上)
  4. 解码+swscale 放大环节仅 32.6 fps,为纯软件栈瓶颈(同上)
  5. 结论:达芬奇 7× 优势来自硬解+GPU 缩放+自研编码器+全图流水线,非 ProRes 编码本身更轻(同上)
- 产物类型:纯文本上下文笔记(无 manifest、无 JSON)
- 主题分组建议:编码导出(上下文参考)
- 可比性备注:非受控基准,含用户侧实测数字;引用只作差距归因叙事,不得当性能数据;与 HISTORICAL 表中 `davinci_context/` 条目对应(各期 dirty 工作树)。

## e1_encode

- 时间:2026-09-26 至 09-28
- 测试对象:E1 编码边界 —— ToposVideoEncoder 输入准备 → native 编码 → mux(排除源解码/GPU/Resolve);7 个构建目录共 44 个 result.json,线程扩展(t1/t4/t8/t12/t16)+ 44 个 probe_rate_*.mov 码率探针样本
- 关键指标:
  1. 4K 4444xq:t16 约 30–34.5 ms/帧 wall(p50 32–37),t8 约 44,t1 约 200–226(`e1_encode/c1245ec6/d5fb9ad5/c9fb38a7/.../result.json` 全部 18 条)
  2. t12 33.6–37.0 vs t16 33.1–34.1 ms —— 12 线程以上饱和(`e1_encode/1d30609f/7b0cc4d4/4232ea12/.../result.json`)
  3. 4K hq:t16 15.9 ms/帧(native 13.7 + mux 1.5 + py 0.05)vs t1 110.6(`e1_encode/44784357/d052edae/2709a8be/20260927T120446882365Z/result.json`)
  4. 2K hq:t16 5.5–5.9 vs t1 37.4 ms/帧(`e1_encode/a3748229/.../result.json`)
  5. 阶段拆分:native 占 wall 80–90%,mux 1.4–4.8 ms/帧,python <0.1(各 clean 构建 result.json)
  6. probe_rate_*.mov 44 个,码率聚在 ~36/61/66/100 Mbps 四簇(文件名 rate 数字 35614–99959)
- 产物类型:manifest 化 JSON(阶段拆分 + 帧级 p50/p95/p99 + 峰值 RSS)+ 探针 mov
- 主题分组建议:编码导出|native域与线程(线程扩展数据在此)
- 可比性备注:7 个构建仅 44784357、a3748229 为 clean,其余 dirty;跨构建绝对值不可比,只可看构建内线程曲线;frames 仅 4–16 帧(短窗)。

## edge_jag

- 时间:2026-09-21(02:40–10:54 UTC 多批)
- 测试对象:边缘锯齿取证 —— topos vs ProRes 边缘质量(grid_residual_ratio / stair_axis_ratio / sharpness_retention / RGB 观感),叠加 qp6、P6 变体、v0–vf 变体与速度批
- 关键指标:
  1. stair_axis_ratio:topos 0.319–0.360 vs ProRes 0.314–0.351,双方均低于 0.6 阈值——无阶梯化签名(`edge_jag/edge_jag_report.json` 2K 四档 mean)
  2. grid_residual_ratio:topos 1.154–1.357 vs ProRes 1.035–1.333,topos 8×8 块界误差集中度略高(同上)
  3. sharpness_retention:topos 1.309–1.522 vs ProRes 1.427–1.657(同上)
  4. RGB 观感:2K proxy mean rgb_luma_psnr_edge topos 35.3 vs ProRes 36.3 dB(`edge_jag/edge_jag_rgb_report.json`)
  5. qp6 EXP vs BASE:基本无变化(2K hq grid 1.166→1.184、psnr_edge 43.76→43.70)(`edge_jag/qp6/metrics_*_{BASE,EXP}.json`)
  6. 变体速度批:2K proxy passthrough 编码 251 fps;解码 worker 扫描 w1 46 → w16 492 fps(`edge_jag/speed/speed_all.json`);门禁 `gate_after_id4.json` ok=true 无违规
- 产物类型:取证 JSON + PNG 可视化 + 变体 bench(无统一 bench_manifest)
- 主题分组建议:画质
- 可比性备注:与 HISTORICAL "各期 dirty 工作树"同类,变体 v0–vf 为并行候选(非交替 A/B),只可排序不可写倍数;PNG 为定性证据。

## export_baseline

- 时间:2026-09-17(01:08–05:38 UTC)
- 测试对象:产品导出路径 4 编码器横评(h264_yuv420p / prores_422 / dnxhr_lb / topos_proxy),3 种源(2K H264 480 帧、2K topos 240 帧、4K H265 120 帧)
- 关键指标:
  1. 2K H264 源:topos_proxy 67.0 fps vs h264 149.4 / dnxhr_lb 59.7 / prores_422 15.6(`export_baseline/export_baseline_2K_H264.json`)
  2. 2K topos 源:topos_proxy 157.2 fps vs dnxhr_lb 80.3 / h264 61.9 / prores 15.6(`export_baseline/export_baseline_2K_topos.json`)
  3. 4K H265 源:topos_proxy 45.1 fps vs dnxhr_lb 27.1 / h264 23.4 / prores 4.5(`export_baseline/export_baseline_4K_H265.json`)
  4. 输出体积:topos_proxy 每组最小(36.3/18.0/37.2 MB;ProRes 437/225/414 MB)(同上)
  5. 24 帧小样本轮(`export_baseline_2K_topos_proxy.json`)fps 6–11,启动开销主导,仅验证通路
- 产物类型:JSON 结果 + 4 个产物 mov 及其导出 manifest
- 主题分组建议:编码导出
- 可比性备注:JSON 仅 platform 标注,无 git/dylib manifest(HISTORICAL 口径不合格);单次运行无重复;源时长不一(480/240/120 帧),跨源 fps 不可直接比。

## extended_real_tests_20260917

- 时间:2026-09-17
- 测试对象:真实素材扩展测试大杂烩:20+ 份 pytest 回归日志 + 真实媒体探针 + 合成/调色/编辑特效节点基准(RX 6800, GL 4.1)+ 离屏播放遥测
- 关键指标:
  1. pytest_media_without_vt:1 failed / 2879 passed / 61 skipped(122.5 s)(`extended_real_tests_20260917/pytest_media_without_vt.log`)
  2. pytest_ai_editing:10 failed / 1913 passed;换 static-ffmpeg 后 1923 passed(`pytest_ai_editing*.log`)
  3. 合成节点全量:注册 93 节点逐一冷启/preview/export 计时(`real_topos_all_composite_nodes.json`);如 grade@hd preview 10.1 ms、export 51.3 ms(`real_topos_composite_nodes_bench.json`)
  4. 编辑特效矩阵:25 行 = 6 passed / 10 separate_domain / 4 not_attachable / 4 exception / 1 timeout(`real_topos_edit_effect_matrix.json`)
  5. 离屏播放:presented p50 39.9 ms / p95 51.4 ms(budget 40),effective_fps 47.8,120/120 帧 CPU 回退(fallback_reasons: no_gpu_pipeline)(`edit_tracks_offscreen.log`)
  6. 多份日志(composite/feature_suites 等)无结束汇总行——运行被截断
- 产物类型:pytest 日志 + 节点基准 JSON + 特效产物 mov(带 manifest)
- 主题分组建议:杂项(回归 + UI/合成管线)
- 可比性备注:非受控基准(无 git/dylib 锚定,日志路径指向旧仓 `topos_1`);性能数字仅域内参考;回归结论随代码演进已过期。

## first_frame_repro

- 时间:2026-09-17(04:20–04:51 UTC)
- 测试对象:首帧问题缺陷复现素材集:11 个 .mov(354 KB–227 MB)+ 逐一配套的导出 manifest(时间线/资产/能力注册/预检/交付 QC),按管线阶段命名(h264 源、prores/topos 中间级、pt 直通等)
- 关键指标:
  1. repro 时间线:1920×1080、24 fps、12 帧、单资产 2K_topos.mov(`first_frame_repro/repro_topos.mov.manifest.json`)
  2. 直通样本 topos_pt/topos_pt2/topos_stage:25 fps、240 帧(对应 manifest)
  3. 全部 preflight ok、blocking=0;delivery_qc passed,topos_stage/prores_stage 各 1 条 media.fps_mismatch 警告(各 manifest `preflight`/`delivery_qc`)
  4. 导出配置:H264 或 PRORES_422,single_clip,ENTIRE,burn_subtitles=true(各 manifest `config`)
  5. 每个输出含 sha256 锚定(如 repro_topos.mov=c929bce4…)(manifest `output`)
- 产物类型:缺陷复现产物 + 导出 manifest(非基准数据)
- 主题分组建议:杂项(缺陷复现)
- 可比性备注:无可执行基准、无计时指标;价值在于带 manifest 锚定的最小复现语料,可配合 export_baseline 同日产物交叉定位;引用时以文件 sha256 为准。

---

## 组 2 汇总速览

| 目录 | 时间 | 分组 | 一句话 |
|---|---|---|---|
| b0907366 | 09-07 | codec | 2K core/E2E 全面领先 ProRes/DNxHR(CI 合格);4K 轮 dirty 无 CI |
| c0f7068d | 09-03 | codec | topos 全档自测矩阵,无参照,Δ 全 n/a |
| c4082663 | 09-03 | codec/AB演进 | 熵编码 V2 较 V1 同画质省 23–27% 码率;10 代损一致 |
| cbf6b308 | 09-03 | codec | 三方全矩阵:编码 4–7× 快,同码率画质差 1.8–3.9 dB |
| d9050239 | 09-03 | codec | CI 质量最高的 core/E2E 对照:4K 编码领先、2K 持平 |
| d9552407 | 09-03 | 画质 | 纯质量 RD vs ProRes:2K 差 1.8–3.0 dB,444 域差约 6 dB |
| codec_bench | 09-03/09-16 | codec | 混合口径历史 harness + M1 六档实测(全档超实时) |
| d1_playback | 09-15 | 播放 | D1 边界:2K proxy 707 fps → 4K 4444xq 40 fps,RSS 746 MiB |
| davinci_context | 09-02 | 编码导出 | 达芬奇 7× 差距归因笔记(硬解+GPU+流水线),非基准 |
| e1_encode | 09-26~28 | 编码导出/线程 | E1 边界线程扩展:t1→t16 提速 6–7×,12 线程饱和 |
| edge_jag | 09-21 | 画质 | 无阶梯签名(stair<0.4),块界集中度略高于 ProRes |
| export_baseline | 09-17 | 编码导出 | 产品导出横评:topos_proxy 档 45–157 fps 且体积最小 |
| extended_real_tests_20260917 | 09-17 | 杂项 | 回归日志+93 合成节点+特效矩阵+离屏播放遥测 |
| first_frame_repro | 09-17 | 杂项 | 首帧缺陷复现语料(11 mov+manifest,无计时) |
