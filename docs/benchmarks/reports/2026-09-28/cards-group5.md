# 数据卡片 · 组5（bench_out/topos_* 目录 + 根级散件 + benchmarks/）

> 计划05（基准分析）P0 数据卡片，2026-09-28。覆盖 bench_out/ 第 5 组 15 个 `topos_*` 目录与根级散件、`benchmarks/` 两个 JSON、根级样例媒体 manifest。
> 有效性总纲见 `bench_out/HISTORICAL.md`：除 `058d29da/` 结果树外均为历史观察；相对结论需同 clean HEAD、同 dylib SHA、交替 A/B + 中位数 + bootstrap CI。

---

## topos_real_bench_2026-09-13

- **时间**：2026-09-13（文件时间戳）
- **测试对象**：TOPOS 原生编解码真实素材速度矩阵（2K/4K × 6 档位，yuv422p10le 4 档 + 10/12-bit 444 两档），单次 120 帧样本；另含现有 TOPOS MOV 完整顺序解码与 8v16 线程对比
- **关键指标**：
  - 2K 编码 fps：Proxy 382.1 / LT 246.5 / Standard 185.6 / HQ 132.1 / 4444 65.5 / 4444 XQ 50.4；解码 fps 622.3→94.7（`report.md` 主表）
  - 4K 编码 fps：Proxy 161.4 / LT 102.5 / Standard 78.6 / HQ 53.2 / 4444 24.9（≈实时）/ XQ 21.4（低于实时）（`report.md` 主表）
  - 码率/bpp：4K HQ 531.4 Mb/s @2.563 bpp；4K XQ 1301.7 Mb/s @6.277 bpp（`report.md`）
  - 现有 MOV 全解码：2K_topos.mov（116.5s/2912 帧）134.6 fps；4K_topos.mov（12s/300 帧）74.6 fps（`report.md` 第二节，`full_mov_decode.json`）
  - 线程对比 4K HQ：交替序 [8,16,16,8,8,16]，8 线程编码 29.9–33.6 fps vs 16 线程 49.9–51.1 fps，码率恒定 531.4 Mb/s（`threads_8v16.json`）
  - 平台：Mac Pro i9-12900T 16C/24T、AVX2+FMA、native 0.1.0（`report.md` 头部）
- **产物类型**：report.md + results.json（12 档位）+ full_mov_decode.json + threads_8v16.json
- **主题分组建议**：编解码吞吐基线（真实素材）；线程扩展性
- **可比性备注**：单次 120 帧样本、无重复无 CI；源路径指向旧仓 `topos_1/tests/test_videos/`；编码计时不含源解码与 MOV mux，不可与完整导出口径直接对比。历史观察级

## topos_timeline_export_2026-09-13

- **时间**：2026-09-13（部分诊断文件 09-22 补入）
- **测试对象**：无效果时间线端到端导出性能（TimelineExporter，planar 直通路径 vs 正常渲染回退），2K/4K 各一次完整导出；附 4K 编码器组件分解诊断
- **关键指标**：
  - 2K：487 帧 7.36 s，全程 66.2 fps / 活跃 101.4 fps，134.0 Mb/s；4K：743 帧 24.86 s，全程 29.9 / 活跃 38.4 fps，531.2 Mb/s（`report.md` 主表）
  - 直通帧数 487/487、743/743 全部 planar 直通，首尾帧可解码（`report.md`，`results_product_policy_full_final.json`）
  - 4K 组件分解（120 帧）：解码 33.4 ms/帧（≈29.9 fps）vs 编码循环 23.3 ms/帧（42.97 fps），native 编码 20.6 ms、mux 2.5 ms（`diagnostic_4k_components.json`）
  - 时间线活跃 38.4 fps ≈ 完整编码子流程（43.0 fps）的 89%（`report.md` 对比段）
- **产物类型**：report.md + run_full_timeline_export.py + 10 余个 results_*.json 分期数据 + 3 组 .topos 工程/导出 MOV manifest
- **主题分组建议**：端到端导出性能；导出管线组件分解
- **可比性备注**：每分辨率单次运行、无重复；源路径同为旧仓 `topos_1/`；与 `topos_real_bench_2026-09-13` 的纯编码口径（4K HQ 53.2 fps）刻意区分（含解码+mux）。历史观察级

## topos_vs_ref

- **时间**：2026-09-01 至 2026-09-02（20 个运行目录）
- **测试对象**：Topos vs 参考编解码器（ProRes HQ / DNxHR HQX，ffmpeg）编码与解码吞吐对照；后期演进为"静机同窗成对交错重测"记分牌修正
- **关键指标**（`20260902_prores_quiet_remeasure.txt` 判定节）：
  - 2K 解码公平比值（Topos hq 内核 / ProRes slice 锚）：0.935 / 0.942 / 0.938 三运行互证，绝对 fps 442.6–454.6 vs 锚 470.6–482.2
  - 2K 同码率档：Topos standard（127.6 Mbps）480.2 fps vs ProRes HQ（107 Mbps）480.3 fps，比值 0.998（打平）
  - 4K 反超修正为 +13~15%（hq，1.130/1.147）与 +21%（std，1.208）；原脏机口径 1.36 系高估
  - 锚点静机位移：2K ProRes 448.2→470.6~482.2（+5~8%），4K 76.1→92.3~93.1（+21~22%）
  - 产品逐帧口径比值：2K 0.771 / 4K 0.967；DNxHR 辅助臂 2K 1.217 / 4K 1.619
  - 示例编码侧（`20260901_195404_4k/result.json`）：Topos 4K hq rate t16 4.12 fps @625.9 Mb/s vs ffmpeg ProRes HQ raw 16.53 fps @655.8 Mb/s
- **产物类型**：每运行目录 result.json + ffmpeg 逐臂日志（ref_*.log）；5 个 quiet 运行含逐对 fps 与 load 采样；1 个方法论文本
- **主题分组建议**：跨编解码器对照；解码记分牌锚点；基准方法论（成对同窗口径）
- **可比性备注**：被 `HISTORICAL.md` 明确列为历史观察（各期 dirty 工作树、无统一 manifest）；元数据自报 `topos_commit: db656b3e`、CPU "Genuine Intel 0000"（i9-9900K 24T）。quiet 重测的价值在"成对比值"而非绝对 fps；结论措辞应保留其自判的修正语义

## topos_codec_stages

- **时间**：2026-09-19（155730 = 2K，155810 = 4K）
- **测试对象**：TOPOS 编解码分阶段计时：encode_only（已解平面→Topos 编码）、transcode（解+编逐帧分解）、decode_out（输出解码），Topos 源文件 96 帧 × 3 passes、16 编码线程
- **关键指标**：
  - 2K（`20260919_155730/results.json`）：proxy 编码中位 397.8 fps / 转码 108.5 fps / 出站解码 642.6 fps；hq 171.7 / 80.6 / 217.3；源 decode_only 中位 135.5 fps
  - 4K（`20260919_155810/results.json`）：proxy 162.8 / 47.4 / 211.3；hq 46.3 / 27.2 / 67.3；decode_only 65.4 fps
  - 转码分解（2K proxy）：解码 6.3–7.3 ms/帧 vs 编码 2.1–2.3 ms/帧，解码占主导
- **产物类型**：2 个 results.json（含逐 pass fps/ms 列表与输出字节）
- **主题分组建议**：编解码吞吐分解（阶段级）
- **可比性备注**：源路径指向旧仓 `topos_1/`，无 manifest/dylib SHA 锚定；3 passes 取中位但无 CI。历史观察级

## topos_same_vs_cross

- **时间**：2026-09-19（8 个运行，15:43–16:22）
- **测试对象**：同族（Topos→Topos，topos_direct_planar_cpu_encode 路径）vs 跨族（Topos 源→ProRes 422 / H.264，ffmpeg 路径）转码对照，96 帧，reps 1–6
- **关键指标**（各 `*/results.json` 的 overall_fps_median）：
  - 2K 同族：topos_proxy 94.9–111.4 fps、topos_hq 80.1–104.7 fps；4K 同族：proxy 37.6–47.2、hq 24.5–35.0
  - 2K 跨族：prores_422 9.3 fps（87.8 MB）、h264 7.9 fps（9.5 MB）；4K 跨族：prores_422 2.2 fps（267.2 MB）、h264 2.5 fps（`20260919_154423`、`154652`）
  - 同族与跨族差距 >10×（4K）；Topos 臂全程走 planar 直通 CPU 编码，ref 臂走 ffmpeg
  - 4K topos_hq 输出 253.9 MB vs proxy 29.0 MB（同源同帧数）
- **产物类型**：8 个 results.json（含逐 rep wall_s/segment_fps/delivery_path/output_bytes/verify）
- **主题分组建议**：跨编解码器对照；planar 直通收益
- **可比性备注**：源路径为旧仓 `topos_1/`，无 manifest；154353 为 reps=1 试点、160113 单臂补测，样本量不齐；rep0 有明显预热坑（如 4K proxy 23.9→44.5 fps），横向引用建议取 reps≥3 的运行。历史观察级

## topos_quality

- **时间**：2026-09-20（baseline-p0、recal-p2、p3 AQ A/B、主观截图）至 2026-09-28（recal-p5）
- **测试对象**：Topos 各档位对 4K ProRes 4444 master 的重编码质量基线与多轮复测（PSNR/MAE/SSIM/block_index，stride 采样），以及空间 AQ 开/关 A/B
- **关键指标**：
  - recal-p5（99 帧采样，`quality_recal-p5.json`）：2K proxy Y-PSNR 35.68 / SSIM 0.9287 → hq 43.01 / 0.9863 → 4444 43.73 / 0.9895；4444xq Y-PSNR 44.14 但 MAE 14.34（同型异常，基线亦然）
  - p0→p2 重校准：proxy Y-PSNR 31.39→35.73（+4.3 dB）、SSIM 0.834→0.929，4444/4444xq 两轮完全一致（`quality_baseline-p0.json` vs `quality_recal-p2.json`）
  - p4（stride 5/99 帧）与 p5 数值几乎相同、耗时 1322.4 s vs 959.3 s（`quality_recal-p4.json` / `-p5.json`）
  - 空间 AQ A/B（`p3_spatial_aq_ab.json`，2K proxy）：AQ 使 Y-PSNR −0.18 dB、SSIM −0.004、编码 fps 266.7→247.2（−7.3%），码率 +0.2%，未见收益
  - 主观截图 6 张（`p2_subjective_*.png`，200x/400x 三源对照）
- **产物类型**：4 个质量基线/复测 JSON + 1 个 AQ A/B JSON + 6 张主观对比 PNG
- **主题分组建议**：压缩质量基线；AQ 消融；主观对照
- **可比性备注**：master 为 `tests/test_videos/4K_prores4444.mov`、无 dylib/manifest 锚定；p0 与 p2 之间发生了档位重校准（proxy 提升最大），跨 tag 数值不可直接平均；4444xq 的 MAE 异常（≈14 vs 4444 的 4.1）疑口径问题，引用需附注

## topos_vt_12bit_ramp_probe（v1）

- **时间**：2026-09-28 08:46 生成、16:47 汇总 JSON
- **测试对象**：12-bit 合成 ramp（yuv444p12le → ProRes p416le 输入 → 4444 XQ → 解码回 12-bit）的码值映射保真探针；显式标注 `hardware_acceleration_confirmed: false`（软件编码器路径）
- **关键指标**（`vt_12bit_ramp.json`）：
  - 128×128、2 帧；Y 通道 mean_error −0.0293 code、MAE 0.150 code、max_abs 1 code；U/V 两通道 0/0/0 完全精确
  - 源/编码/解码 SHA-256 齐全（8fc2e379… / 7321a052… / 3fb0a5cf…）
  - 状态自标 `synthetic_mapping_probe_no_quality_verdict`
- **产物类型**：探针 JSON + 源/解码 yuv、p416le raw、5.7 KB prores mov
- **主题分组建议**：12-bit 精度/映射探针
- **可比性备注**：合成素材、2 帧微型样本，无质量判定语义；未记录 ffmpeg 版本与脚本 SHA（v2 补齐）。可作映射机制证据、不可作压缩质量结论

## topos_vt_12bit_ramp_probe_v2

- **时间**：2026-09-28 08:47 生成、16:47 汇总 JSON
- **测试对象**：同 v1 实验的复现加固版：新增 `measurement_script_sha256`（a97ab678…）与 `ffmpeg_version`（ffmpeg 9.0.2），其余协议不变
- **关键指标**（`vt_12bit_ramp.json`）：
  - 三个 SHA-256 与逐通道指标与 v1 逐位一致（Y MAE 0.150/max 1，U/V 精确），即复现成功
  - `hardware_acceleration_confirmed: false`、`allow_software_fallback: true` —— 结论限定于软件编码器
- **产物类型**：同 v1（探针 JSON + yuv/raw/mov 三件套）
- **主题分组建议**：12-bit 精度/映射探针（与 v1 归并展示，v2 为可复现性证据）
- **可比性备注**：v1/v2 数据相同，引用以 v2 为准（带脚本与 ffmpeg 版本锚定）；仍为合成探针、无 release 判定语义

## topos_native_domain_smoke

- **时间**：2026-09-27 23:29–23:37（raw.json 元数据 16:47 补写）
- **测试对象**：native codec CLI（toos）新原生域的冒烟验证素材：raw12 / raw16 Bayer 四相位 + float16 / float16-finite 源文件（128×128），供后续 matrix 测试的公共源
- **关键指标**：
  - 冒烟样本 raw12-4 编解码往返：`raw12-4.dec.raw.json` 头部确认 width/height 128、bit_depth 12、pixel_format 3、channels R,Gr,Gb,B、uint16 LE planar、`concealed_slices: 0`
  - float16 侧车（同目录 float16-finite.raw 98304 B = 128×128×3×2 B）供 matrix/audit 复用
- **产物类型**：5 个合成源 raw + 1 组往返产物（toos/raw/raw.json）
- **主题分组建议**：原生域（RAW/float16）支持验证——冒烟前置素材
- **可比性备注**：仅是源素材与单点冒烟，无逐档指标；正式数值在 `topos_native_domain_matrix`。合成域、无 release 判定

## topos_native_domain_matrix

- **时间**：2026-09-27 15:30–15:37（UTC）生成，raw.json 元数据 09-28 补写
- **测试对象**：native codec CLI（toos，SHA 06508c5c…）在 4 个合成输入域 × 多档位的往返质量/压缩矩阵：raw12（6 档）、raw16（6 档）、float16（4 档）、float16-finite（4 档），128×128
- **关键指标**（各子目录 `native_domains_*.json`）：
  - raw12：raw12-2 压缩比 1.55× / PSNR_min 108.4 dB / max_abs 1 code；raw12-16 4.14× / 56.7 dB / max_abs 30
  - raw16：raw16-2 1.48× / 96.8 dB / max_abs 3；raw16-16 5.85× / 50.8 dB / max_abs 849
  - float16：float-ultra 2.84× / max_half_ulp 2；float-low 10.14× / max_half_ulp 1102（有损档半精度 ULP 漂移大）
  - float16 vs float16-finite：编码字节逐档完全相同，差异仅在特殊类计数（含非有限值源 scc=1–2，finite 源 scc=0）
  - 全部 20 档 `bit_exact: false`；状态自标 `synthetic_or_input_domain_measurement_no_release_verdict`
- **产物类型**：4 子目录 × 每档（toos + 解码 raw + raw.json 头）+ 4 个汇总 JSON（含 encode/decode 命令与全量 SHA）
- **主题分组建议**：原生域（RAW/float16）档位质量-压缩曲线
- **可比性备注**：自带 toos CLI SHA + 测量脚本 SHA + 源 SHA 锚定（较早期目录规范），但非 P0-04 manifest 结果树、合成 128×128 素材、每档单次；档位间趋势可比，绝对值不可外推到真实素材

## topos_native_domain_float_zero_audit

- **时间**：2026-09-28 00:42（UTC）
- **测试对象**：float16 域零值/特殊类专项审计：finite 源（docs/codec/evidence/TOPOS_2026-09-27_local_pilot/float16-finite.raw）经 4 档编解码后，对 ±0、零↔非零跃迁、特殊类（Inf/NaN）变化的逐通道核查；toos CLI 已换为 333b7df3…（比 matrix 的 06508c5c 新）
- **关键指标**（`native_domains_float16.json`）：
  - 压缩比与 matrix finite 版逐档一致（ultra 2.84× … low 10.14×，编码字节相同）
  - float-ultra：zero_sign_changes=1（G 通道），special_class_changes=0；float-high/medium/low：source_zero_to_nonzero=1（零→非零各 1 例）
  - 全档 source_nonzero_to_zero=0、negative_zero_to_nonzero=0、special_class_changes=0 —— 无 Inf/NaN 类变化、无负零产生、无非零塌缩为零
  - max_half_ulp：ultra 2 / high 5 / medium 199 / low 1102；全档 all_bit_exact=false
- **产物类型**：4 档 ×（toos + 解码 raw + raw.json）+ 1 汇总 JSON（含完整 encode/decode 命令行）
- **主题分组建议**：原生域 float16 语义正确性审计（零/特殊值）
- **可比性备注**：与 matrix/float16-finite 同源同码流，可交叉验证；CLI SHA 与 matrix 不同（升级后复测），跨目录比较时注意二进制版本；合成域、`no_release_verdict` 自标

## topos_raw_real_cfa_probe

- **时间**：2026-09-28 09:25–09:26（UTC+8 文件时间），汇总 JSON 01:26 UTC
- **测试对象**：真实 ProRes RAW 素材中心裁切 2048×2048 RGGB raw16，过 native codec CLI 六档（raw16-2…16）往返质量/压缩——真实 CFA 数据版的 native domain 矩阵
- **关键指标**：
  - 裁切源统计（`crop_manifest.json`）：全帧 70.78 MB、crop_xywh [3072,1136,2048,2048]、四相位均值 R 1317 / Gr 1749 / Gb 1748 / B 613、最大 39168（Gr）、crop SHA 03961f03…
  - 压缩比（`raw16_six_tiers/native_domains_raw16.json`）：raw16-2 2.54×（96.9 dB）→ raw16-16 102.4×（60.8 dB）——真实低熵 raw 下高档压缩比远超合成（合成 raw16-16 仅 5.85×）
  - 误差：raw16-2 max_abs 5 code；raw16-16 max_abs 889 / MAE 42.6；全档 bit_exact=false
- **产物类型**：全帧 rggb16le + 裁切 raw + crop_manifest.json + 六档 toos/raw/raw.json + 汇总 JSON
- **主题分组建议**：原生域（RAW）真实素材档位曲线——与 native_domain_matrix 成对（合成 vs 真实）
- **可比性备注**：自带源/CLI/脚本 SHA；真实素材但单裁切单次、2048×2048 一块、无重复；与合成矩阵趋势方向一致但绝对压缩比差异巨大（素材熵不同），必须分组呈现不可混排

## topos_paired_q01_synthetic

- **时间**：2026-09-27 15:05–15:06（UTC）
- **测试对象**：同源配对压缩实验（合成）：同一 yuv 源分别编 Topos（native dylib SHA d052edae…）与 ffmpeg prores_ks 同 profile，比较码流大小与对源的逐帧 PSNR/MAE；320×192 × 8 帧，4 组（proxy / 4444xq / 4444-vt-12bit / 4444xq-vt-12bit）
- **关键指标**（各 `paired_*.json`）：
  - proxy（422p10）：码流比 topos/prores = 0.944；Y-PSNR topos 46.35 vs prores 41.41（+4.9 dB）
  - 4444xq（444p12，VT 前）：码流比 1.349；Y-PSNR 79.53 vs 47.47
  - 4444xq-vt-12bit（VT 同 12-bit 口径）：码流比 0.992；Y-PSNR 77.27 vs 47.54
  - 4444-vt-12bit：码流比 0.973；Y-PSNR 70.76 vs 47.51
  - 每组含源/native 库/两侧 mov 的 SHA-256 与完整 ffmpeg 命令行；状态 `measurement_only_no_release_verdict`
- **产物类型**：4 组 × paired JSON + topos/prores 双侧 mov（含 p416le 输入 raw）+ 2 个源 yuv
- **主题分组建议**：同源配对压缩对照（合成）；VT 12-bit 口径对齐方法
- **可比性备注**：合成 8 帧微样本、码率未配平（-vt-12bit 系列即为此做的口径对齐前身）；prores 侧为 ffmpeg prores_ks 而非 Apple VideoToolbox。可作方向性证据

## topos_paired_q01_real_source

- **时间**：2026-09-27 15:09–15:56（UTC）
- **测试对象**：同源配对压缩实验（真实素材）：`2K_prores4444XQ.mov` 解出的 lossy-source 30 帧 1920×1080，Topos 各档 vs ffmpeg prores_ks 同名 profile；`-match` 系列为 2% 内码率配平复测，`proxy-crf30/55/63` 为 ProRes 侧不同 CRF 压缩点
- **关键指标**（各 `paired_*.json`，Y-PSNR 为 30 帧均值）：
  - 码率配平组（rate_matched_2pct）：proxy-match 比 1.003 / Y 42.55 vs 35.02；lt-match 0.991 / 47.53 vs 36.49；standard-match 0.988 / 50.55 vs 36.92；hq-match 0.997 / 55.10 vs 37.22；4444xq-vt-12bit-match 1.004 / 64.53 vs 37.32
  - 未配平组（topos 默认档）：proxy 0.813 / 41.33；hq 0.828 / 52.85；4444xq 0.801 / 59.94 —— Topos 默认码率更低且仍大幅占优
  - proxy-crf63（最狠压缩点）：比 7.74×，Y-PSNR 62.36 vs 35.02
  - `4444xq/paired_4444xq.json` 自标 `invalid_reference_encoder_format`：本机 prores_ks 不支持 yuv444p12le 输入被静默降为 10-bit，该文件全部 ProRes 分数无效（正是 -match/VT 系列重测的原因）
  - `4444xq-vt-12bit-match/independent_decode_3f.json`：Apple（p416le 高 12 位）vs ffmpeg（yuva444p12le 前 3 平面）双解码器一致性，Y MAE 0.315 code / max 3
- **产物类型**：15 组 × paired JSON（+1 independent_decode JSON）+ 双侧 mov + 2 个源 yuv（248.8 MB / 373.2 MB）
- **主题分组建议**：同源配对压缩对照（真实素材、码率配平）——Q01 核心证据
- **可比性备注**：引用时只取 `-match`（rate_matched_2pct=true）与 VT 12-bit 组；非 match 的 4444xq 已自判无效，proxy/lt/standard/hq 非 match 组仍有效但码率未配平（Topos 码率约低 17~24%，需注明非等比特）；source_origin=decoded-lossy-source（非无损原始）

## topos_perf_thread_refine_3rounds.json（根级）

- **时间**：2026-09-28（引用 e1_encode/1d30609f/7b0cc4d4/4232ea12/2026-09-28T01:01–01:03Z 各 result.json）
- **测试对象**：编码线程数细化试点（8/12/16），3 轮、轮内序平衡（每轮次序不同），Topos 编码（E1 口径）在 i9-9900K 上的 wall/每帧分解；自标 `local_pilot_not_product_export`
- **关键指标**：
  - 9 次运行中位：8 线程 mean_wall 39.43 ms/帧（p95 48.23）；12 线程 34.37（43.49）；16 线程 33.86（41.63）（`median_by_threads`）
  - native_ms_per_frame：8T ≈33.2 / 12T 27.6–28.8 / 16T 26.9–27.8；mux ≈4.2–6.3 ms 恒定
  - 峰值 RSS 2.35–2.44 GB，随线程数微增
  - 每行内嵌 bench_manifest.v1：git_head 1d30609f（dirty）、dylib SHA 7b0cc4d4…、ABI 2、AVX2、python 3.12.13
- **产物类型**：汇总 JSON（行级引用 e1_encode 不可变结果树的 result.json 路径）
- **主题分组建议**：编码线程扩展性（8/12/16 细化）；E1 编码口径
- **可比性备注**：底层运行来自带 manifest 的 `e1_encode/` 结果树（当前有效体系），但 git_dirty=true 且每组仅 3 样本取中位、无 bootstrap CI——按 HISTORICAL 规则不可写"快/慢 x%"式相对结论，仅作趋势观察；12→16 收益已近饱和（<2%）

## benchmarks/timeline_occlusion_bench_result.json

- **时间**：未内嵌时间戳（文件 mtime 09-22，实际运行早于该日期）
- **测试对象**：时间线遮挡剔除（cull）A/B：2K 双轨素材（lower 2K_h264.mp4 sha1 038491830c2b + upper 2K_prores422HQ.mov sha1 2d6a348afa44），warmup 4 / measure 12 / runs 1
- **关键指标**：
  - 仅 B0_single_track 组：cull_on 70.14 fps vs cull_off 68.29 fps，speedup 1.027，culled_ratio 0，帧请求 16/16
  - 单轨场景无遮挡可剔，speedup 全来自噪声（无 CI）
- **产物类型**：单结果 JSON（含素材 sha1_12 与 all_runs 原始值）
- **主题分组建议**：UI/时间线渲染——遮挡剔除 A/B
- **可比性备注**：runs=1 无重复无 CI；仅单轨组，剔除机制未被激活（request_reduction=0），1.027 的"加速"无意义；4K 版（下一个卡片）才含有效遮挡组。无 manifest/dylib 锚定

## benchmarks/timeline_occlusion_bench_result_4k.json

- **时间**：未内嵌时间戳（同上）
- **测试对象**：同上探针的 4K 版：lower 4K_H264.mov（c90cf62a5f99）+ upper 4K_prores422HQ.mov（7438f4962405），warmup 10 / measure 60 / runs 2，三场景组
- **关键指标**：
  - B0 单轨：cull_on 24.24 fps vs off 23.71，speedup 1.022（无剔除，噪声级）
  - B1 双轨全遮挡：cull_on 11.53 fps（请求 70、culled_ratio 0.5）vs off 3.14 fps（请求 140），**speedup 3.67×、请求减半**
  - B2 双轨上层缩放（无整轨遮挡）：cull_on 2.12 vs off 2.13 fps，speedup 0.994（剔除失效，符合预期）
- **产物类型**：单结果 JSON（三组 × 两臂 × 2 runs 原始数据）
- **主题分组建议**：UI/时间线渲染——遮挡剔除 A/B（核心证据）
- **可比性备注**：runs=2、中位数无 CI；B1 的 3.67× 是该目录最强信号但未达 HISTORICAL 的 bootstrap CI 门槛，宜表述为"观察值"；绝对 fps 与其他目录时间线导出口径不同（此处为渲染层帧请求，非编码导出）

## bench_out 根级样例媒体：cpu_real.mov / gpu_real.mov（+ manifest）

- **时间**：2026-09-15 05:15 UTC（两 manifest created_at 相差 4 秒）
- **测试对象**：GPU vs CPU 渲染路径导出对照的产物样例：同项目名 "gpu-verify"、同素材 `2K_topos_proxy.mov`、1920×1080 / 24 fps / 48 帧 / TOPOS_HQ / 无音频，两次独立导出（cpu_real.mov 69.8 MB、gpu_real.mov 118.2 MB）
- **关键指标**（`cpu_real.mov.manifest.json` / `gpu_real.mov.manifest.json`）：
  - schema `topos.export_manifest.v1`；输出 SHA：cpu 79245bd6…（69,841,258 B）、gpu 143d801a…（118,211,337 B）——两文件不同（码率差异 ~1.69×，本身即是 GPU/CPU 路径差异的观察点）
  - 均含 12 条 capability registry 条目、preflight ok、delivery_qc 结构完整
  - 差异项：cpu manifest 的资产 sha256 为 null（"延后后台生成"警告），gpu manifest 资产 sha256 已生成（4d348e8c…）
- **产物类型**：2 个导出 MOV（大媒体，本卡只读 manifest）+ 2 个 export manifest JSON
- **主题分组建议**：导出产物样例 / GPU vs CPU 渲染路径对照素材（配套分析在 macos_gpu_export_20260915 等目录，属其他组）
- **可比性备注**：manifest 是导出口径（topos.export_manifest.v1）而非基准 manifest（bench_manifest.v1），无计时数据；文件体积差异提示两路径输出比特流不同，引用性能结论需回对应运行目录；媒体本体不应读入分析

---

## 组5 横向备注

1. **规范性梯度**：最规范的是 `topos_perf_thread_refine_3rounds.json`（引用 e1_encode manifest 树）与 09-27/28 的 native domain / paired_q01 / vt_probe / raw_cfa 系列（自带源/CLI/脚本 SHA + 显式 `no_release_verdict` 状态）；09-13/09-19 各目录与 benchmarks/ 均无锚定，属 HISTORICAL 历史观察。
2. **跨仓路径**：`topos_real_bench_2026-09-13`、`topos_timeline_export_2026-09-13`、`topos_codec_stages`、`topos_same_vs_cross`、`threads_8v16.json` 的 source 指向旧仓 `topos_1/`，与当前仓库素材同名的连续性未经验证。
3. **机器跨度**：09-13 系列在 Mac Pro i9-12900T（16C/24T），09-01/02 与 09-28 系列在 i9-9900K（8C/24T 级）——绝对 fps 跨目录比较一律无效。
4. **已知失效数据**：`topos_paired_q01_real_source/4444xq/paired_4444xq.json`（invalid_reference_encoder_format，10-bit 静默降级）；`topos_quality` 中 4444xq 的 MAE 异常；`benchmarks/` 2K 版 cull A/B 无有效剔除组。
