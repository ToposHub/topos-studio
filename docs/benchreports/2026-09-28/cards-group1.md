# bench_out/ 第 1 组目录条目卡（计划05 P0 基准分析）

覆盖：01a7466d、058d29da、5bc83bef、64e7e219、6b3dfe9d（任务单笔误 "6b3dfe6d"，实际目录名为 6b3dfe9d）、6d749586、77b91b28、8efc9a50、94b31cc8、9a8bfd99、a986c3d9（任务单笔误 "a9866d9"）、ab4k_baseline、ab4k_dev、ab_baseline、ab_dev，共 15 个。

通用背景：哈希名目录 = native codec 仓库的 git commit，内部路径为 `<commit>/<dylib_sha256前8>/<manifest哈希8>/<UTC时间戳>/`（HISTORICAL.md P0-04 惯例）。除 058d29da 外均属 P0-04 规则生效前的散点结果（历史观察，仅方向性参考）。

## 01a7466d
- 时间：2026-09-03T19:12Z（路径 UTC 时间戳）
- 测试对象：AQ 重定档（aq_retier）回归验证——验证某次改动后 AQ 重定档对码率应"无操作"（ratio 全 1.0）
- 关键指标（出处：01a7466d/82e0667a/ce11b147/20260903T191204Z/aq_retier.json）：
  - 6 个 tier（proxy/lt/standard/hq/pro444/extreme）× 2k/4k 全部 ratio = 1.0（b_base == b_aq，48 帧）
  - anchor 与 new target bpp 完全一致（proxy 0.60509 → extreme 6.76296 bpp）
  - mbps_1080p25_new：proxy 31.4 / lt 63.9 / standard 97.2 / hq 142.8 / pro444 213.1 / extreme 350.6
  - proxy 2k/4k 与 extreme 2k 触 QP 地板（qp=63，floor_frames=48）
  - git_dirty=false，dylib sha 82e0667a，ABI 2，slice_threads 16
- 产物类型：json
- 主题分组建议：codec（AQ/码率定档子域）
- 备注：无容差判定字段输出（match_pct 均为 0），只能作为"无操作"证据，不可与其他 commit 的 ratio 做跨脚本比较；与 64e7e219 同为 aq_retier 脚本但行为不同（见该卡）。

## 058d29da
- 时间：2026-09-02T23:42Z（路径 UTC 时间戳）
- 测试对象：P0-02 重写后的 core/E2E 双口径基准——topos 编码/解码 vs ProRes HQ 与 DNxHR HQX（仅 2K/hq 档，24 帧）
- 关键指标（出处：058d29da/37659ad5/a5b6fae6/20260902T234241Z/result.json）：
  - core 编码中位数：topos 0.467 s vs prores_hq 0.602 s（ratio 0.777，CI95 [0.753, 0.801]）、vs dnxhr_hqx 1.014 s（ratio 0.462）
  - e2e 编码中位数：topos 0.322 s vs prores 0.695 s（ratio 0.468）、vs dnxhr 1.406 s（ratio 0.232）
  - 解码中位数：topos 0.140 s vs prores 0.325 s（ratio 0.432，CI95 [0.414, 0.449]）
  - 文件大小：topos 23,850,642 B vs prores 12,942,833 B（约 1.84×）
  - git_dirty=true（P0-04 允许的锚定例外：commit+dylib SHA+manifest 断言）
- 产物类型：json
- 主题分组建议：codec（基准锚点）
- 备注：① HISTORICAL.md 指定本目录为"当前有效"结果树的根，新结果按同结构追加；② 数据内 interpretation 字段全部写 "ref faster"，但 ratio_median 均 < 1（topos 实际更快），该字段在 9a8bfd99 中又标 "topos faster"，说明 interpretation 语义在该期间不稳定，引用时以 ratio+CI 为准；③ 仅 2K 单档 24 帧，样本范围窄。

## 5bc83bef
- 时间：2026-09-03T13:37Z（路径 UTC 时间戳）
- 测试对象：RD（率失真）曲线扫描——V1/V2 两代熵编码器 × QP 20–55，2K（422/10bit/profile3）与 4K（444/12bit/profile5），含 10 代重编码世代损失
- 关键指标（出处：5bc83bef/8e335ef7/318dbea5/20260903T133743Z/rd_result.json）：
  - 2K V2@qp45：1,228,629 B/帧、245.7 Mbps、enc 12.2 ms/帧、dec_p50 7.26 ms、PSNR-Y 74.76 dB（V1 同 QP 为 348.5 Mbps——V2 省 29.5% 码率）
  - 4K V2@qp45：2591.8 Mbps vs V1 3370.4 Mbps（同质量 PSNR 77.62 dB）
  - qp≤35 全部精确重建（PSNR=∞，maxerr=0）；qp40 起 maxerr 1–2
  - 世代损失（2K V1 qp45，10 代）：PSNR-Y 74.63 → 72.69 dB，码率 1,759,389 → 1,752,541 B/帧
  - git_dirty=true
- 产物类型：json
- 主题分组建议：codec（RD 曲线/熵编码器对比）
- 备注：dirty 工作树构建，只能作方向性参考；enc/dec 计时为均值口径（enc_ms 单值），无 CI；与 6b3dfe9d 为同一脚本两次跑（6b3dfe9d 干净构建、generations 扩到 qp30+qp45），两者不宜混用。

## 64e7e219
- 时间：2026-09-03T18:16Z（路径 UTC 时间戳）
- 测试对象：AQ 重定档生效验证——AQ 开启后各 tier 码率相对 anchor 的压缩比（与 01a7466d 同脚本、不同 commit 行为）
- 关键指标（出处：64e7e219/48f15e80/480b40ff/20260903T181637Z/aq_retier.json）：
  - ratio（b_aq/b_base）：lt 2k 0.9647、standard 2k 0.9632、hq 2k 0.9617、standard 4k 0.9840、hq 4k 0.9858（AQ 约省 1.5–4% 码率）
  - proxy/extreme 全部 ratio=1.0（QP=63 地板锁死，AQ 无效）
  - mbps_1080p25_new：standard 2k 93.6（anchor 97.2）、hq 2k 137.4（anchor 142.8）
  - SAFE 判定取两分辨率较保守值：standard 0.984、hq 0.9858
  - git_dirty=false
- 产物类型：json
- 主题分组建议：codec（AQ/码率定档子域）
- 备注：b_base 与 01a7466d 完全一致（同 anchor 输入），可与 01a7466d 成对解读"改动前后"；单次运行无重复样本。

## 6b3dfe9d
- 时间：2026-09-03T11:06Z（路径 UTC 时间戳）
- 测试对象：RD 曲线扫描（V1/V2 × QP20–55，2K/4K）+ 世代损失，覆盖 qp30 与 qp45 两组 generation 曲线（6b3dfe9d 为 5bc83bef 的干净构建扩样版）
- 关键指标（出处：6b3dfe9d/24421afb/e5250099/20260903T110654Z/rd_result.json）：
  - 2K V2@qp45：245.7 Mbps、enc 13.3 ms/帧、dec_p50 6.41 ms、PSNR-Y 74.76 dB（与 5bc83bef 码率/质量逐位一致，仅计时有差）
  - 4K V1@qp55：2149.1 Mbps、enc 70.0 ms、dec_p50 20.3 ms、PSNR-Y 67.43 dB
  - generations：每分辨率 4 组（V1/V2 × qp30/qp45）× 10 代
  - git_dirty=false，threads_effective=16
- 产物类型：json
- 主题分组建议：codec（RD 曲线/熵编码器对比）
- 备注：与 5bc83bef 的质量/码率字段完全可复现（确定性编码器），但 enc/dec 计时为不同构建+不同次运行，不可跨目录比较耗时；PSNR=Infinity 在 JSON 中为非标准字面量，机器解析需处理。

## 6d749586
- 时间：2026-09-03T14:04Z（路径 UTC 时间戳）
- 测试对象：topos vs ProRes 全 tier 视觉质量 RD 对比（2K/4K，96 帧素材，422 与 444 两个域，同码率插值 ΔdB）
- 关键指标（出处：6d749586/abd9b794/18a75e89/20260903T140424Z/rd_vs_prores_result.json）：
  - 2K 同码率 Δ（topos−ProRes 插值）：proxy −1.81 dB、LT −3.04 dB；4K：proxy −2.07 dB、LT −2.49 dB、422 −3.86 dB
  - 2K hq：topos 191.25 Mbps/65.46 dB vs prores 106.47 Mbps/65.89 dB（topos 质量接近但码率 1.8×）
  - 444 域 2K：topos pro444 307.7 Mbps/50.97 dB vs prores 4444 290.4 Mbps/56.77 dB（差 5.8 dB）
  - 4K 帧数：frames_total 96、frames_measured 48（4K 只测前 48 帧）
  - topos extreme 为 12bit，其余 10bit
- 产物类型：json
- 主题分组建议：画质（也可归 codec；建议与 codecs_matrix 系列同组作画质对比）
- 备注：Δ 依赖 ProRes RD 曲线插值，HQ 档超插值区间输出 null；引用的素材文件（prores_*/topos_*.mov）不在本目录内，无法核对生成条件。

## 77b91b28
- 时间：2026-09-07T07:00–07:04Z（三次运行：07:00:39Z 空目录[中断]，07:00:53Z 完整，07:04:29Z 补充 444 域）
- 测试对象：Topos × ProRes × DNxHR 三编码器对照矩阵（2K，96 帧@25fps，3 次重复，含配对速度比与同码率插值 Δ）
- 关键指标（出处：77b91b28/e493357f/c1eaa900/20260907T070053Z/codecs_matrix.{md,json} 与 .../2ef81b40/20260907T070429Z/）：
  - 2K 422：topos enc 5.3–8.6 ms/帧 vs prores 26.4–44.1 ms/帧（配对耗时比 0.17–0.22×ProRes），dec 1.6–4.0 ms vs prores 0.8–1.7 ms（2.0–2.5×ProRes，解码更慢）
  - 同码率 Δ（topos−ProRes）：proxy +0.50 dB、lt −0.39 dB、standard −1.44 dB
  - hq 配对（vs DNxHR HQX 10bit）：enc 1.975×、dec 3.289×（topos 更慢）
  - 444 域：topos pro444 214.1 Mbps/50.97 dB vs dnxhr 444 367.0 Mbps/60.20 dB
  - 运行环境：i9-9900K、Python 3.13.5、PyAV 17.0.1/ffmpeg 8.0.1、v6-dev 构建
- 产物类型：json + md（表格报告）
- 主题分组建议：codec（跨编码器对照矩阵）
- 备注：① 与 09-03 系列（PyAV 18.1.0/ffmpeg 8.1.2、"Genuine Intel 0000" 屏蔽 CPU 串、release 构建）环境不同，跨目录耗时不可比；② 本机首次出现真实 CPU 型号（i9-9900K），暗示换了基准机或不再屏蔽 CPUID；③ 070039Z 为空目录（启动失败的残留）；④ 070429Z 运行 git_dirty=true，但 dylib sha 与 070053Z 相同。

## 8efc9a50
- 时间：2026-09-03T17:18Z（路径 UTC 时间戳）
- 测试对象：codecs_matrix 的单点补充运行——仅 4K/44412（12bit 444）extreme 档一行
- 关键指标（出处：8efc9a50/ffbb8075/ed870cc7/20260903T171841Z/codecs_matrix.{md,json}）：
  - topos extreme 4K：1373.2 Mbps、PSNR-Y 61.58 dB、SSIM-Y 0.99941、enc 84.3 ms/帧、dec 22.5 ms/帧（96 帧@25fps，质量测前 48 帧）
  - meta：threads 16、runs 3、PyAV 18.1.0/ffmpeg 8.1.2、entropy v2
  - 无参照编码器行（Δ 全 n/a）
- 产物类型：json + md
- 主题分组建议：codec（对照矩阵残片）
- 备注：单行孤点，疑似矩阵中断后只落盘最后一个 case；1373 Mbps 与 a986c3d9 的 4K extreme（1315 Mbps）同为 extreme@4K 但数值差 ~4%，属不同 commit 构建差异。

## 94b31cc8
- 时间：2026-09-03T17:16Z（路径 UTC 时间戳）
- 测试对象：P4 阶段码率重定档实验（p4_retier）——将各 tier 目标 bpp 旧锚（old_target_bpp）重定到新锚，统计新旧字节比
- 关键指标（出处：94b31cc8/661eed9c/edc9c85e/20260903T171623Z/p4_retier.json）：
  - b2/b1 比值：proxy 2k 0.6763、standard 2k 0.6990、extreme 4k 0.7397（全部约 0.67–0.74，即新定档省 26–33% 码率）
  - proxy 2k：old_target_bpp 0.89 → new 0.6019，mbps_1080p25_new 31.2
  - qp_median：proxy 63（地板）、lt 58、standard 55、hq 51、pro444 59、extreme 57（96 帧逐帧 qp_all）
  - git_dirty=false，无 args 块（与 aq_retier 系脚本不同源）
- 产物类型：json
- 主题分组建议：codec（码率定档）
- 备注：b1/b2 语义未在文件内注明（推测旧/新目标档实际编码字节），跨 commit 不可比；与 01a7466d/64e7e219 的 aq_retier 是三套不同的定档实验，勿混。

## 9a8bfd99
- 时间：2026-09-04T14:18Z（路径 UTC 时间戳）
- 测试对象：core/E2E 双口径速度对比复测——topos vs ProRes HQ/DNxHR HQX，2K+4K 各 48 帧 hq 档（与 058d29da 同结构脚本的扩展版）
- 关键指标（出处：9a8bfd99/58730875/f21fc30a/20260904T141812Z/result.json）：
  - 2K core 编码 ratio：vs prores 0.861（CI95 [0.851, 0.889]）、vs dnxhr 0.637；e2e ratio：0.465 / 0.355
  - 4K core 编码 ratio：vs prores 0.691（CI95 [0.672, 0.702]）、vs dnxhr 0.547；e2e ratio：0.384 / 0.296
  - 解码 ratio：2K 0.80（vs prores）、4K 0.622 —— 全部 interpretation "topos faster" 且 CI 不跨 1
  - 文件大小：4K topos 138,090,679 B vs prores 157,149,380 B（topos 更小，与 058d29da 相反）
  - dylib 路径来自独立工作树 /Users/testuser/Documents/vscode/topos_1/（非主仓 topos）
- 产物类型：json
- 主题分组建议：codec（速度对比/AB 演进）
- 备注：本目录所有 ratio 与 058d29da（两天前，同机、全 "ref faster"）方向完全相反，是组内最意外的一张卡；两目录 dylib 构建自不同工作树（topos vs topos_1），无 manifest 断言链可证明可比，引用"速度翻盘"前需先确认两次的参照（ffmpeg/PyAV）版本一致。

## a986c3d9
- 时间：2026-09-03T16:59Z（路径 UTC 时间戳）
- 测试对象：topos 单编码器全 tier 矩阵（无参照行）——2K/4K × 422/44410/44412 六组，tier×分辨率完整扫描
- 关键指标（出处：a986c3d9/5846622a/9215ca5e/20260903T165919Z/codecs_matrix.{md,json}）：
  - 2K 422：proxy 32.6 Mbps/57.86 dB → hq 136.8 Mbps/65.47 dB；enc 8.7–14.2 ms/帧、dec 2.4–4.8 ms/帧
  - 4K 422：proxy 208.8 Mbps → hq 547.8 Mbps；enc 32.2–44.1 ms/帧
  - 4K 44412 extreme：1315.4 Mbps/60.36 dB/enc 70.5 ms/dec 20.8 ms
  - meta：runs 3、PyAV 18.1.0、entropy v2、96 帧@25fps（4K 质量测前 48 帧）
- 产物类型：json + md
- 主题分组建议：codec（topos 自身基线矩阵）
- 备注：质量/码率数值与 77b91b28（09-07，v6-dev 构建）的 topos 行完全一致（如 2K proxy 32.6/57.86），说明 tier 参数未变；但 enc/dec 耗时系统性更高（proxy enc 8.7 vs 5.3 ms），构建配置与 PyAV 版本双重差异，不可比耗时。

## ab_baseline
- 时间：2026-09-17T05:01Z（manifest created_at，成对导出 baseline 侧）
- 测试对象：first-frame-repro 项目（1920×1080、48 帧、25fps、烧字幕、无音频）经 4 种导出编码器（H264/PRORES_422/DNXHR_LB/TOPOS_PROXY）的 baseline 侧产物
- 关键指标（出处：ab_baseline/*.mov.manifest.json，schema topos.export_manifest.v1）：
  - 文件大小：h264 1,865,492 B、dnxhr_lb 9,044,809 B、topos_proxy 3,675,091 B、prores_422 44,268,180 B
  - delivery_qc 全部 passed（blocking/warning/info = 0）
  - 导出顺序 05:01:08 h264 → 05:01:13 prores → 05:01:17 dnxhr → 05:01:21 topos_proxy（单次顺序会话）
- 产物类型：媒体（.mov）+ json manifest
- 主题分组建议：AB演进
- 备注：纯导出产物对（无计时/质量 json），用于与 ab_dev 做字节级/像素级 diff；manifest 含完整 capability registry 快照（multicam/嵌套序列等 P1 功能），可佐证当时应用功能面；baseline 与 dev 侧 project_id 不同（a07b49c3 vs ef94904a），是两次独立重建的项目而非同一工程文件。

## ab_dev
- 时间：2026-09-17T05:01Z（manifest created_at，与 ab_baseline 相隔 7–21 秒，成对导出 dev 侧）
- 测试对象：同 ab_baseline 的 4 编码器导出，dev 构建侧产物（first-frame-repro，1080p/48 帧/25fps）
- 关键指标（出处：ab_dev/*.mov.manifest.json）：
  - 文件大小：h264 1,862,428 B（−0.17% vs baseline）、dnxhr_lb 9,044,809 B（逐字节相同）、topos_proxy 3,677,337 B（+0.06%）、prores_422 44,067,859 B（−0.45%）
  - delivery_qc 全部 passed
  - dnxhr_lb 两侧 sha 应相同（字节数一致），其余三个编码器字节数有微小漂移
- 产物类型：媒体（.mov）+ json manifest
- 主题分组建议：AB演进
- 备注：无逐帧质量或计时数据，差异判定需另行对 .mov 做解码对比；prores_422 差 200 KB 属编码器非确定路径还是真实回归，仅凭字节数无法定论。

## ab4k_baseline
- 时间：2026-09-17T05:42–05:44Z（manifest created_at）
- 测试对象：first-frame-repro 项目 4K 版（3840×2160、24 帧、25fps）baseline 侧导出，仅 H264 与 TOPOS_PROXY 两编码器
- 关键指标（出处：ab4k_baseline/*.mov.manifest.json）：
  - h264.mov 3,523,383 B（05:44:36Z）；topos_proxy.mov 7,098,377 B（05:42:46Z）
  - 两文件均带完整导出 manifest（sha256、delivery_qc）
  - timeline：3840×2160、duration_frames=24、fps=25
- 产物类型：媒体（.mov）+ json manifest
- 主题分组建议：AB演进
- 备注：与 ab_baseline 相隔 40 分钟、帧数不同（24 vs 48），是独立的新时间线而非同项目改分辨率；manifest 中 output.path 用相对路径（"bench_out/..."），与 ab_* 系列的绝对路径写法不同，提示导出代码路径有变更。

## ab4k_dev
- 时间：2026-09-17T05:43–05:44Z（manifest created_at，与 ab4k_baseline 交错生成）
- 测试对象：同 ab4k_baseline 的 4K/24 帧导出，dev 构建侧（H264 + TOPOS_PROXY）
- 关键指标（出处：ab4k_dev/*.mov.manifest.json）：
  - h264.mov 3,484,567 B（−1.10% vs baseline，4K 下差异比 2K 的 −0.17% 放大）
  - topos_proxy.mov 7,135,617 B（+0.52% vs baseline）
  - 两编码器导出间隔仅 ~1 分钟（05:43:50 与 05:44:36 附近）
- 产物类型：媒体（.mov）+ json manifest
- 主题分组建议：AB演进
- 备注：4K H264 两侧 39 KB 差值远大于 2K 对（3 KB），若做 AB 结论应以解码后像素为准；同样没有配套计时 json。
