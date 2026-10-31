# G6 A/B 演进主题分析（ab-evolution.md）

> 计划 05 P1 · 组 G6，2026-09-28。基于 `docs/benchreports/2026-09-28/cards-group*.md` 数据卡 + `bench_out/` 原始数据（只读，关键数字已二次抽查核实）。
> 覆盖：`ab_baseline` vs `ab_dev`、`ab4k_baseline` vs `ab4k_dev`、`m8`、`m9`、`ai_upgrade`、`p0_runner`、`p2_09`、`topos_anchor_matrix_20260917`、`extended_real_tests_20260917`、哈希目录中有 AB 性质者（`01a7466d`+`64e7e219`、`058d29da`、`9a8bfd99`、`b0907366`、`c4082663`、`d9050239`、`94b31cc8`）。
> 有效性总纲（`bench_out/HISTORICAL.md`，P2-08）：相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 不跨 1.0。本组绝大多数 A/B 为 dirty 树 + 无 manifest 的历史观察——**比值方向可引用，"快/慢 x%"的严格表述仅限满足门槛的少数运行**（表内"可信度"列逐一标注）。

---

## 0. 结论速览

1. **codec 层最大单笔收益是熵编码 V1→V2**：同画质省 23–31% 码率（m9 全 6 格 −27.9%~−31.7%，c4082663 4K qp45 −23.0%/qp60 −27.0%），且重建逐位一致。
2. **产品导出层最大单笔收益是 T-P1-10 直通快路径**：identity→topos_proxy 10.15→80.05 fps（7.9×），随后 T-P1-11（slice_rows 16→8）继续 80.05→95.02 fps；同一场景的独立锚点链 9.6（09-17 19:38）→ 91.4（09-18）。
3. **GPU 效果链解锁（T-P1-21）**：grade 1.38→33.47 fps（24.3×）等 7 场景全部过门槛——基线"forced_gpu 与 cpu 同速"是 GPU 从未生效的结构性缺陷。
4. **回归信号总体干净**：字节级 ab 对仅 KB 级漂移、无 QC 失败；pytest 快照 1–10 个失败均定位到环境（VT/ffmpeg）而非产品回归；T-P1-11 码率/质量门禁全过。
5. **9a8bfd99/058d29da "速度翻盘"未锚定，禁止引用**（§4）。

---

## 1. 各阶段 A/B 收益总表

按时间排序。ratio 语义：耗时比（<1 = topos 更快）。出处均为 `bench_out/` 下相对路径。

| 阶段/日期 | A/B 臂 | 关键指标（结果） | 出处 | 可信度 |
|---|---|---|---|---|
| M8 全矩阵（08-31） | topos vs ProRes HQ vs DNxHR HQX（2K/4K，冷启/解码/编码/画质） | 4K 随机解码 12.72 ms vs ProRes 73.15 / DNxHR 51.88（快 4–6×）；RSS max 186.4 vs 582.6/952.7 MB（省 3–5×）；4K 编码 82.77 ms(qp57) vs ProRes 240.9（vs DNxHR 30.8 慢）；4K PSNR-Y 60.39 vs 65.58/64.07 dB；gen10 多代 59.65 dB > ProRes 57.15；t1/t2/t4/t8 packet_bytes 逐位一致 | `m8/decode_4k.json`、`m8/gen_4k.json`、`m8/quality_4k.json`、`m8/cold_4k.json` | dirty 树，机器未记录；方向性 |
| M9 V2 验收（08-31，lib_commit 93e72e84） | v1 vs v2 全链路（2K/4K × qp40/50/57，48 帧 × 3 reps） | 码率 −27.9%~−31.65%（6/6 格）；解码提速 1.00–1.17×；编码 p50 打平；recon_parity 6/6 true | `m9/vlc_acceptance.json` | 同 lib_commit 内分流，无 CI；与 5bc83bef RD（−29.5%@2K qp45）互证 |
| aq_retier 前后（09-03） | 01a7466d（改动后"无操作"）vs 64e7e219（AQ 生效） | 01a7466d：12 格 ratio 全 1.0（b_base==b_aq，QP 地板格除外）；64e7e219：lt/standard/hq ratio 0.9617–0.9858（AQ 省 1.5–4% 码率）；两跑 b_base 逐格一致（同 anchor 输入） | `01a7466d/82e0667a/ce11b147/20260903T191204Z/aq_retier.json`、`64e7e219/48f15e80/480b40ff/20260903T181637Z/aq_retier.json` | 均单次无重复；manifest 完整（clean、dylib SHA 齐）；成对解读可作"改动前后"，但无 CI |
| V1/V2 RD（09-03，c4082663） | 同轮内 V1 vs V2（4K 44412，qp25–60，48 帧） | qp45：V2 2591.96 vs V1 3368.80 Mbps（−23.0%），psnr_y 同 77.589；qp60：1285.47 vs 1760.58（−27.0%），同 60.476；gen10 77.521→73.456 dB 两代际同轨迹 | `c4082663/0df35db5/7973290a/20260903T132512Z/rd_result.json` | dirty 树；确定性编码器质量可比，计时不可比 |
| core/E2E 对照链（09-03→09-04） | d9050239 → 058d29da → 9a8bfd99 → b0907366（topos vs ProRes/DNxHR） | 详见 §2.2 CI 质量表 | 各 `*/*/*/result.json` | 逐目录见 §2.2 |
| p4 定档（09-03，94b31cc8） | 新旧 target_bpp 定档（b2/b1） | proxy 2k 0.6763、standard 2k 0.6990、extreme 4k 0.7397（省 26–33% 码率）；qp_median proxy 63（地板）…extreme 57 | `94b31cc8/661eed9c/edc9c85e/20260903T171623Z/p4_retier.json` | clean+manifest；b1/b2 语义文件内未注明（推测为旧/新档字节），跨 commit 不可比 |
| ab_baseline vs ab_dev（09-17 05:01Z） | 产品导出 4 编码器 baseline vs dev（first-frame-repro，1080p/48 帧） | h264 1,865,492→1,862,428 B（−0.17%）；dnxhr_lb 逐字节相同；topos_proxy 3,675,091→3,677,337（+0.06%）；prores_422 44,268,180→44,067,859（−0.45%）；delivery_qc 8/8 passed | `ab_baseline/*.mov.manifest.json`、`ab_dev/*.mov.manifest.json`（字节数已用 `ls` 核实） | 无计时/质量 json；字节漂移无法定论（§3） |
| ab4k_baseline vs ab4k_dev（09-17 05:42–44Z） | 4K/24 帧导出（H264+TOPOS_PROXY） | h264 3,523,383→3,484,567 B（−1.10%）；topos_proxy 7,098,377→7,135,617（+0.52%） | `ab4k_*/ *.mov.manifest.json` | 同上；4K 差异比 2K 放大（39 KB vs 3 KB） |
| p0_runner（09-16，3 次运行） | 跨 codec 播放矩阵 + TOPOS 全档扫描（非改动 A/B，阶段锚点） | 稳态 p50：2K h264 2.234 ms（447.6 fps）、4K hevc 22.451、4K prores422HQ 31.175、4K DNxHR 25.759、4K topos 13.227、4K topos_proxy 4.715 ms（212.1 fps）；随机 seek：4K hevc 376.1 ms vs topos 12.9 / topos_proxy 4.6 ms（29–80×）；TOPOS 2K 档 proxy 1.351→4444xq 7.031 ms、4K 档 4.637→22.465 ms；longrun 600 帧 RSS 增量 ≤13.4 MB | `p0_runner/p0_runner_20260916_005041.json`、`_005645.json`、`_005815.json` | repeats=1 无 CI；run1 与 run2/3 commit 不同（7b13746e vs df8e2847）；锚点级 |
| p2_09 会话池（09-16） | 单流 vs 16 流轮换（LRU cap=12 + lease） | h265_4k 单流 39.9 fps → 热流 128.3 fps（p50 0.15 ms）、冷首帧 35.2 ms；prores_4k 单流 44.5 → 热流 46.9、冷首帧 165.3 ms（比 h265 贵 4.7×）；evictions=4、重建 ok | `p2_09/p2_09_pool_20260916_114756.json`（采信 114837 的剔除预热口径） | 行为验证级，无 commit/dylib 锚定 |
| anchor matrix（09-17 19:38） | TOPOS 422Proxy 全链路锚点 + 7 效果场景 × 2 选路 | codec 锚点：解码 433.92 / 隔离编码 147.17 / 逐帧 decode→encode 96.57 fps；timeline identity cpu 9.598 / forced_gpu 9.775 fps；效果倍率 grade 6.02×、two_layer 3.23×、sharpen 2.38×、glow 2.37×、transition 2.22×、blur 1.73×；14 组合 GPU trail 全部 `cpu_encoder_surface_path` | `topos_anchor_matrix_20260917/topos_anchor_matrix.md` + child_*.json | runs=1；"不能算全链路 GPU"（文件自述） |
| T-P1-10 直通（09-17 23:25） | 改前 vs 改后（identity→topos_proxy） | 10.15 → 80.05 fps（7.9×）；达 codec-only 90.22 的 88.7%；delivery_path=topos_direct_planar_cpu_encode（诚实标注 CPU 编码） | `ai_upgrade/T-P1-10_20260917-232524/result.md` | 48+8 帧协议同机同素材；stash A/B，无 CI |
| T-P1-11 slice_rows 16→8（09-18） | sr16 vs sr8（36 组合矩阵 + 锚点） | 2K t16 编码 121.80→170.28（+39.9%）、4K +5.2%、2K 解码 204.8→294.5（+43.8%）；码率 +≤1%、roundtrip Δ=0.0；锚点 codec-only 90.22→101.42（+12.4%）、identity 80.05→95.02（+18.7%） | `ai_upgrade/T-P1-11_20260918-061227/result.md` + encode_stage_matrix.json | 矩阵 8+48 帧/组合；质量/码率门禁过 |
| T-P1-21 GPU 链解锁（09-18） | 基线 forced_gpu（=CPU 同速缺陷）vs 修复后 | grade 1.38→33.47（24.3×）、blur 7.1×、sharpen 10.4×、glow 9.2×、two_layer 12.0×、transition 8.3×；identity 87.12→86.54 持平（直通路径不受影响） | `ai_upgrade/T-P1-21_20260918-085120/result.md` | 同上；"forced_gpu 与 cpu 同速"为结构性缺陷（TypeError 每帧回落）之证据 |
| current_anchor（09-18） | 全链路锚点复测 | 原生解码 415.06 / 编码 159.33 / decode→encode 101.45 fps；timeline identity cpu 91.37 / forced_gpu 83.58；grade cpu 1.60 vs gpu 36.62 fps | `ai_upgrade/current_anchor_20260918/topos_anchor_matrix.md` | 与 09-17 矩阵同协议；identity 9.6→91.4 的链式改进跨 3 个改动 |
| T-P0-31 全 GPU 链（09-19） | 冷/暖进程（240 帧 1080p H.264） | 暖 93.26 fps（≥60 达标）、冷 49.69；阶段 p50 decode 2.9 + render 9.2 + encode_submit 0.18 ms；full_gpu_blockers 首次为空 | `ai_upgrade/T-P0-31_20260919-015545/result.md` + perf.json | 3×80 帧拼接；以 015545 为准（其余小目录为脚本迭代中间态） |
| W/A2 回归（09-19） | W01–W08 真实工作流 + A2 缓存格式复验 | W 矩阵 8/8 PASS；A2 零回归；T-P1-71 H265 200 轮 slope 2.199 MB/轮 <5 判有界 | `ai_upgrade/W_matrix_20260919-141056/result.md`、`A2_regression_20260919-141050/`、`T-P1-71_20260919-110914/` | 功能/稳定性口径，非计时 A/B |

注：m10/m11 系列的编解码内核 A/B（缓存 −16%、流水线 +54%、负结果回退档案等）按分组归 G2/G4 报告，本表不重复；本报告仅在 §2.5 链条中引用其终点数字。

---

## 2. 分层明细

### 2.1 codec 层：V1→V2 熵编码与定档

- **V1→V2 是唯一同时被三个独立目录佐证的收益**：m9（6/6 格 −27.9~−31.65%，`m9/vlc_acceptance.json`）、c4082663（4K qp45 −23.0%/qp60 −27.0%，同 psnr）、5bc83bef/6b3dfe9d（2K V2@qp45 省 29.5%，`rd_result.json`，出处见数据卡组 1）。重建一致性（recon_parity）全 true。
- **AQ 定档**：01a7466d 证明改动后 AQ 重定档对码率"无操作"（ratio 全 1.0，含 QP=63 地板格）；64e7e219 为同脚本另一 commit 的生效轮（0.9617–0.9858）。两跑 b_base 一致支持"同 anchor 输入、不同行为"的成对解读。
- m8 的 sized 模式码率误差 −20.54% 与后续 ±0.5%（m11_stage1，G4/G2 详）不可直接比较——RC 已换代（数据卡组 3 备注）。

> **冲突注记（BENCH-0011，2026-09-29 追注）**：空间 AQ 的收益证据**互相冲突**——本节 64e7e219 的 aq_retier 显示 AQ 省 1.5–4% 码率（ratio 0.9617–0.9858，manifest 完整但单次无 CI），而 G5 quality.md §1.2 的 p3_spatial_aq（`bench_out/topos_quality/p3_spatial_aq_ab.json`，2K/stride 20/26 帧每臂）显示同码率下**三项全负**（ΔPSNR −0.182/−0.268 dB、编码 fps −7.3%~−12%、码率 +0.03%~+0.2%）。两轮协议不同（retier 全矩阵单次 vs p3 单素材单次 A/B、均无 CI、无交替轮），冲突未定，**两边的"AQ 有/无收益"表述均不得单独引用**。复现通道：`tools/bench_spatial_aq_ab.py`——同素材同码率目标（tier bpp 码控）aq off/on 两臂**逐轮交替**（`--rounds`，默认 3），输出 PSNR/编码 fps/码率三项 + 成对 bootstrap 95% CI + 预注册判据（码率配平门 2%、ΔPSNR_Y 门 ±0.1 dB、fps 比值 CI 下限 ≥0.90），结果带 manifest 落 `bench_out/spatial_aq_ab/` P0-04 树；`--dry-run` 可无媒体自检。截至本注记日**裁决数据尚未采集**。

### 2.2 速度对照口径演进（哈希目录链）

同族脚本（core/E2E 双口径 vs ProRes HQ / DNxHR HQX）四个时间点的 CI 质量（全部由 result.json 原文核实）：

| 目录/日期 | 状态 | 2K core enc ratio vs ProRes（CI95） | 4K core enc ratio | 解码 ratio | 文件大小 T vs P |
|---|---|---|---|---|---|
| d9050239（09-03） | clean，黑苹果，16 线程 | 0.984 [0.948, 1.002]（indistinguishable） | 0.897 [0.872, 0.903]（topos faster） | 2K indist. / 4K 0.764 | T ≈1.9× P（2K 96.6 vs 51.1 MB） |
| 058d29da（09-02 23:42） | **dirty（P0-04 允许的锚定例外）**，当前有效树根 | 0.777 [0.753, 0.801] | —（仅 2K） | 0.432 [0.414, 0.449] | T 1.84× P（23.85 vs 12.94 MB） |
| 9a8bfd99（09-04） | clean，但 dylib 来自独立工作树 topos_1 | 0.861 [0.851, 0.889] | 0.691 [0.672, 0.702] | 2K 0.80 / 4K 0.622 | 4K：T 138.1 < P 157.1 MB（与 058d29da 反向） |
| b0907366（09-07） | 2K clean（threads_eff=4，i9-9900K）；4K dirty 无 CI | 0.439 [0.429, 0.448]（e2e 0.285） | 0.380（CI=None，indistinguishable） | 2K 0.318 [0.315, 0.320] | 2K：T 5.50 vs P 4.30 MB |

- **实测**：满足"clean + CI 不跨 1"门槛、可写相对结论的只有 d9050239（黑苹果）与 b0907366 的 2K 轮（threads_eff=4，与同期 16 线程结果不可横比）。
- **实测**：跨目录 ratio 数值波动大（2K core 0.439→0.984），由构建、线程数、机器三方混杂——这正是 P0-04 统一 manifest 规则的动因。
- 058d29da 的 interpretation 字段全部写 "ref faster" 而 ratio_median <1（topos 实际更快），该字段语义当期不稳定，引用以 ratio+CI 为准（数据卡组 1 备注①，本次复核证实）。

### 2.3 产品导出字节对（ab_*）

- **实测**：4+2 个导出产物全部 delivery_qc passed、manifest 完整（schema topos.export_manifest.v1）。dnxhr_lb 两侧逐字节相同；h264 −0.17%（2K）/−1.10%（4K）、prores_422 −0.45%、topos_proxy +0.06%（2K）/+0.52%（4K）。
- **实测**：baseline 与 dev 侧 project_id 不同（a07b49c3 vs ef94904a），是两次独立重建的项目而非同一工程文件；ab4k 的 output.path 用相对路径，提示导出代码路径有变更。
- **推测**：prores_422 差 200 KB 与 4K h264 差 39 KB 属编码器非确定路径还是真实回归，仅凭字节数无法定论——需解码后像素对拍（数据卡组 1 判断）。

### 2.4 播放锚点（p0_runner / p2_09）

- p0_runner 提供的是**阶段锚点**而非改动 A/B：跨 codec 矩阵（run1，commit 7b13746e）与 TOPOS 全档扫描（run2/3，commit df8e2847）协议固定（warmup 10 / steady 120 / seek 30 / longrun 600）。
- 最强信号：随机 seek p50——4K hevc 376.1 ms（max 625）vs 4K topos 12.9 / topos_proxy 4.6 ms（`_005041.json` seek_random）；TOPOS 全档 seek ≈ steady（帧内随机访问结构优势）。
- p2_09：会话池 LRU（cap=12）行为符合设计（16 流轮换 evictions=4、重建 ok）；冷首帧成本排序 ProRes 165.3 ms > h265 35.2 ms（4.7×）。
- **回归信号（负面观察）**：无。longrun RSS 增量全部 ≤13.4 MB（多数 <4 MB，DNxHR 为 0）。

### 2.5 时间线导出与 GPU 链条（09-17 → 09-19，最强演进链）

同一"identity 时间线导出 topos_proxy 输出"场景的跨改动链（各点独立实测，出处见 §1 表）：

| 时点 | FPS | 变化 | 驱动 |
|---|---:|---|---|
| 09-17 19:38（anchor matrix） | 9.598（cpu）/ 9.775（forced_gpu） | — | 基线：GPU 从未生效 + 无直通路径；14 组合 trail 全部 cpu_encoder_surface_path |
| 09-17 23:25（T-P1-10 改后） | 80.05 | 7.9× | 直通快路径（planar 零转换直达编码器） |
| 09-18（T-P1-11 改后） | 95.02 | 再 +18.7% | slice_rows 16→8 |
| 09-18（current_anchor） | 91.37（cpu）/ 83.58（forced_gpu） | 锚点复测 | 独立运行，量级吻合 |
| 09-19（T-P0-31，H.264 输出口径） | 93.26（暖，全 GPU 链） | full_gpu_blockers 首次为空 | IOSurface 解码→Metal NV12→VT 编码 |

- **实测**：效果场景同期从 grade 1.60（cpu）提升到 forced_gpu 36.62 fps（current_anchor），与 T-P1-21 的 24.3× 一致。
- **实测**：T-P1-21 基线"forced_gpu 与 cpu_render 完全同速（grade 1.38/1.40）"证明 GPU 链此前从未生效——这是"标签≠路径"的方法论教训，09-17 anchor matrix 的 trail 字段（全部 cpu_encoder_surface_path）即为诚实标注体系的起点。

### 2.6 回归/稳定性快照（extended_real_tests_20260917）

出处：`extended_real_tests_20260917/pytest_*.log`（尾行核实）等。

| 项 | 结果 | 备注 |
|---|---|---|
| pytest_media_without_vt | 1 failed / 2879 passed / 61 skipped（122.5 s） | 失败：test_frame_provider_vt_zero_copy（VT 排除口径下的会话默认值） |
| pytest_ai_editing | 10 failed / 1913 passed | 换 static-ffmpeg 后 1923 passed 全绿（`pytest_ai_editing_with_static_ffmpeg.log`）→ 环境问题非产品回归 |
| pytest_feature_suites / non_composite_suites | 无结束汇总行 | 运行被截断（崩溃栈），不可当失败率引用 |
| 合成节点矩阵 | 93 节点；T-P1-60 系 77 passed_nonflat / 0 不可解释 | `real_topos_all_composite_nodes.json`、ai_upgrade/T-P1-60 |
| 编辑特效矩阵 | 25 行：6 passed / 10 separate_domain / 4 not_attachable / 4 exception / 1 timeout | `real_topos_edit_effect_matrix.json` |
| 离屏播放 | presented p50 39.9 ms（budget 40）/ effective_fps 47.8；120/120 帧 CPU 回退（no_gpu_pipeline） | `edit_tracks_offscreen.log`——早于 T-P1-21 修复 |

**归类**：回归结论随代码演进已过期（无 git/dylib 锚定，日志路径指向旧仓 topos_1）；价值是 09-17 时点的功能面快照与"static-ffmpeg 治愈 ai_editing 失败"的环境证据。

---

## 3. 回归信号清单（全组扫描）

| # | 信号 | 强度 | 判定 |
|---|---|---|---|
| R1 | ab_dev prores_422 −0.45%（−200 KB）、4K h264 −1.10%（−39 KB） | 字节漂移 | **待验证**：无像素对拍，非确定性路径与真实回归不可分（§2.3） |
| R2 | topos_proxy 体积 +0.06%（2K）/+0.52%（4K） | 微弱 | 与 T-P1-11 码率门禁 +≤1% 同量级，暂不视为回归 |
| R3 | pytest_media 1 failed（VT zero-copy 会话默认值） | 单点 | 环境相关（without_vt 口径），非产品回归证据 |
| R4 | pytest_ai_editing 10 failed → static-ffmpeg 后 0 failed | 已闭环 | ffmpeg 环境问题 |
| R5 | extended_real_tests 离屏播放 120/120 帧 CPU 回退（no_gpu_pipeline） | 已闭环 | 09-17 时点 GPU 链未生效，T-P1-21（09-18）修复 |
| R6 | 9a8bfd99 vs 058d29da 方向冲突（§4） | 高 | 未锚定，不作为回归或进步证据 |
| R7 | topos_quality 2K 4444xq MAE 异常 | 画质口径 | 见 G5 quality.md §6.3，非 A/B 回归 |

**实测**：满足门槛的正收益改动（T-P1-10/11/21、T-P0-31、V1→V2）均有质量/位流门禁（roundtrip Δ=0、recon_parity、码率 +≤1%）伴随——回归信号总体干净。

---

## 4. 翻盘标注：9a8bfd99 / 058d29da 不可引用

`058d29da`（09-02 23:42Z，dirty，dylib 37659ad5，主仓 topos 工作树）与 `9a8bfd99`（09-04 14:18Z，clean，dylib 58730875，**独立工作树 topos_1**）为同族脚本前后两天运行：

- **实测差异**：① interpretation 标签全部相反（058d29da 全 "ref faster" vs 9a8bfd99 全 "topos faster"），而两文件 ratio_median 均 <1（topos 更快）——标签语义当期不稳定；② 2K core ratio 0.777 vs 0.861；③ 文件大小方向反转：058d29da topos 为 ProRes 的 1.84×，9a8bfd99 4K topos（138,090,679 B）反而小于 ProRes（157,149,380 B）；④ 帧数与档位范围不同（24 帧 2K 单档 vs 48 帧 2K+4K）。
- **为何不可引用**：两次 dylib 构建自不同工作树（topos vs topos_1），无 manifest 断言链可证明参照（ffmpeg/PyAV）版本一致；HISTORICAL 规则 2 的可比性前提（同 clean HEAD + 同 dylib SHA + 交替 A/B）不成立。
- **处置**：任何"速度翻盘/反超"叙事禁止引用这对目录；058d29da 仅作为 P0-04 不可变结果树的**结构根**有效，其数字引用须带 dirty 标注与 2K 单档 24 帧的样本范围注。

---

## 5. 实测结论与推测分列

**实测**
1. §1 总表全部带出处的比值/数值（其中满足 clean+CI 门槛的严格相对结论仅 d9050239、b0907366 2K 轮）。
2. 直通链 9.6→80.05→95.02 fps 与 GPU 效果链 1.38→33.47 fps 的量级（各自目录内 A/B，同机同素材同协议）。
3. 回归信号 R1–R7 的现状描述。
4. 9a8bfd99/058d29da 的四处实测差异（§4）。

**推测 / 待验证**
1. ab_* 对的字节漂移归因（R1：非确定路径 vs 回归）——需解码像素对拍。
2. aq_retier 两目录"改动前后"的因果解读（b_base 相同支持但无 CI）。
3. p0_runner run2/3（df8e2847）相对 run1（7b13746e）无改动说明，档位扫描数值只能作锚点、不可作演进推断。
4. "identity 9.6→91.4 的链式改进全部来自 T-P1-10/11"——三个时点为独立运行，贡献拆分未经同窗交替 A/B 验证。
