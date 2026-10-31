# G7 杂项归档主题分析（misc.md）

> 计划 05 P1 · 组 G7，2026-09-28。基于 `docs/benchreports/2026-09-28/cards-group*.md` 数据卡 + `bench_out/` 原始数据（只读，关键数字已二次抽查核实）。
> 覆盖：`davinci_context`、17 个哈希命名目录中**非 AB 性质**者、根级 `cpu_real.mov` / `gpu_real.mov` 媒体清单；附 `benchmarks/timeline_occlusion_*` 归类附注。
> 有效性总纲（`bench_out/HISTORICAL.md`，P2-08）：哈希目录 = native codec 仓库 git commit，内部路径 `<commit>/<dylib_sha256前8>/<manifest哈希8>/<UTC时间戳>/`（P0-04 惯例）；除 `058d29da/` 外均属 P0-04 规则生效前的散点结果（历史观察，仅方向性参考）。

---

## 0. 结论速览

1. 17 个哈希目录按性质二分：**8 个有 AB/演进性质**（归 G6，见 `ab-evolution.md`），**9 个为单方测量/矩阵**（本报告归档，主要供 G3 codec 与 G5 画质引用）。
2. 哈希目录群的机器构成混杂（黑苹果 "Genuine Intel 0000" → i9-9900K 真名），且多个 manifest 的 dylib_path 指向旧仓（topos / topos_1）——**绝对值跨目录一律不可比，这是 P0-04 之前的系统性缺陷**。
3. `davinci_context` 是唯一的"外部参照归因"档案：达芬奇 7× 优势来自硬解+GPU 缩放+自研编码器+流水线，非 ProRes 编码本身更轻；数字只作叙事，不作性能数据。
4. `cpu_real.mov` / `gpu_real.mov` 是导出产物样例（非基准数据）：同项目同素材两次导出体积差 ~1.69×（69.8 vs 118.2 MB），本身就是 GPU/CPU 路径输出比特流不同的观察点；性能结论须回对应运行目录。

---

## 1. 哈希目录归类总表（17 个全量）

AB 性质判定标准：目录内含"改动前后/双臂/双实现对照"结构（V1 vs V2、old vs new、A vs B、双工作树对照）。判定为 AB 者**不再在本报告展开**，仅指回 G6。

| 哈希目录 | 时间（UTC 戳） | 性质 | 一句话结论 | 主要出处 | 去向 |
|---|---|---|---|---|---|
| 058d29da | 09-02 23:42 | **AB**（速度对照；当前有效树根） | core/E2E vs ProRes/DNxHR 基线锚点（dirty 例外） | `058d29da/37659ad5/a5b6fae6/20260902T234241Z/result.json` | G6 §2.2/§4 |
| 9a8bfd99 | 09-04 14:18 | **AB**（速度对照复测） | 与 058d29da 方向冲突、未锚定，禁引 | `9a8bfd99/58730875/f21fc30a/20260904T141812Z/result.json` | G6 §4 |
| b0907366 | 09-07 | **AB**（2K clean + CI 合格） | 2K core ratio 0.439 [0.429,0.448] topos faster | `b0907366/63f18e17/8623acfd/.../result.json` | G6 §2.2 |
| d9050239 | 09-03 | **AB**（CI 质量最高的对照） | 4K 编码 topos faster 0.897，2K indistinguishable | `d9050239/c9e31afc/8c36c356/.../result.json` | G6 §2.2 |
| c4082663 | 09-03 13:25 | **AB**（同轮 V1 vs V2） | 4K qp45 V2 省 23.0% 码率同画质 | `c4082663/0df35db5/7973290a/20260903T132512Z/rd_result.json` | G6 §2.1 |
| 01a7466d | 09-03 19:12 | **AB**（aq_retier 无操作验证） | 12 格 ratio 全 1.0 | `01a7466d/82e0667a/ce11b147/20260903T191204Z/aq_retier.json` | G6 §2.1 |
| 64e7e219 | 09-03 18:16 | **AB**（AQ 生效轮） | ratio 0.9617–0.9858（省 1.5–4%） | `64e7e219/48f15e80/480b40ff/20260903T181637Z/aq_retier.json` | G6 §2.1 |
| 94b31cc8 | 09-03 17:16 | **AB**（新旧定档对比） | b2/b1 = 0.67–0.74（新档省 26–33%）；b1/b2 语义未注明 | `94b31cc8/661eed9c/edc9c85e/20260903T171623Z/p4_retier.json` | G6 §1 表 |
| 5bc83bef | 09-03 13:37 | 非 AB（RD 扫描，dirty） | 2K V2@qp45 245.7 Mbps 省 29.5%；与 6b3dfe9d 质量逐位一致仅计时不同 | `5bc83bef/8e335ef7/318dbea5/20260903T133743Z/rd_result.json` | 本报告 §2.1 / G3 |
| 6b3dfe9d | 09-03 11:06 | 非 AB（RD 扫描，clean 扩样） | 干净构建 + generations 扩 qp30/qp45；质量字段可复现 | `6b3dfe9d/24421afb/e5250099/20260903T110654Z/rd_result.json` | 本报告 §2.1 / G3 |
| 6d749586 | 09-03 14:04 | 非 AB（画质 vs ProRes） | 同码率 Δ：2K proxy −1.81 / LT −3.04 dB；444 域差 5.8 dB | `6d749586/abd9b794/18a75e89/20260903T140424Z/rd_vs_prores_result.json` | 本报告 §2.2 / G5 边界注 |
| d9552407 | 09-03 12:46 | 非 AB（纯质量 RD vs ProRes） | 4K Δ −2.068/−2.494 dB；2K hq topos 1.8× 码率近同质 | `d9552407/ce8ba9ab/c2bd6715/20260903T124654Z/rd_vs_prores_result.json`（prores_interp_deltas 已复核） | 本报告 §2.2 / G5 边界注 |
| 77b91b28 | 09-07 | 非 AB（三方矩阵，i9-9900K） | 2K 编码 0.17–0.22×ProRes、解码 2.0–2.5×；Δ proxy +0.50 dB | `77b91b28/e493357f/c1eaa900/20260907T070053Z/codecs_matrix.{md,json}` | 本报告 §2.3 / G3 |
| cbf6b308 | 09-03 15:25 | 非 AB（三方矩阵，clean） | 编码 4–7× 快；Δ −1.8~−3.9 dB；与 c0f7068d 构成 dylib 前后 | `cbf6b308/e124e93c/90f1bf4f/20260903T152528Z/codecs_matrix.md` | 本报告 §2.3 / G3 |
| a986c3d9 | 09-03 16:59 | 非 AB（topos 自身矩阵） | 2K proxy 32.6 Mbps/57.86 dB → hq 136.8/65.47；与 77b91b28 topos 行逐位一致（tier 参数未变） | `a986c3d9/5846622a/9215ca5e/20260903T165919Z/codecs_matrix.{md,json}` | 本报告 §2.3 / G3 |
| c0f7068d | 09-03 16:47 | 非 AB（topos 自身矩阵） | 2K hq 190.9 Mbps/68.61 dB；黑苹果；无参照 Δ 全 n/a | `c0f7068d/00be27d2/557a6269/20260903T164719Z/codecs_matrix.md` | 本报告 §2.3 / G3 |
| 8efc9a50 | 09-03 17:18 | 非 AB（矩阵残片） | 仅 4K 44412 extreme 单行：1373.2 Mbps/61.58 dB（与 a986c3d9 同档差 ~4%，构建差异） | `8efc9a50/ffbb8075/ed870cc7/20260903T171841Z/codecs_matrix.{md,json}` | 本报告 §2.3 |

**归类结论（实测层面）**：
1. 哈希目录 17 个全部完成二分：8 AB（→G6）+ 9 非 AB（本报告 §2 展开）。任务单中 `cb4f3089`/`6b3dfe6d`/`a9866d9` 均为笔误，实际为 `cbf6b308`/`6b3dfe9d`/`a986c3d9`（已核对，数据卡组 1/2 注明）。
2. 时间聚集：09-03 一天内 11 个目录（P0-04 规则 09-03 落地前的大扫除），09-04/09-07 各为复测与换机（i9-9900K 首次出现真实 CPU 型号）。
3. `058d29da` 是唯一"当前有效"树根；其余 16 个均历史观察。

---

## 2. 非 AB 哈希目录要点归档

### 2.1 RD 曲线扫描对（5bc83bef dirty / 6b3dfe9d clean）

- 同一脚本两次跑：**质量/码率字段逐位可复现**（2K V2@qp45 同为 245.7 Mbps / PSNR-Y 74.76 dB），仅计时不同（enc 12.2 vs 13.3 ms/帧）——确定性编码器的证据（数据卡组 1）。
- qp≤35 精确重建（PSNR=∞、maxerr=0），qp40 起 maxerr 1–2；世代损失 2K V1 qp45 十代 74.63→72.69 dB。
- 引用规则：两目录**不宜混用**；计时跨目录不可比；PSNR=Infinity 为非标准 JSON 字面量，机器解析需处理。
- 归档去向：G3 codec（码率-画质关系主表）。

### 2.2 纯画质对照对（6d749586 / d9552407）

- 同码率插值 Δ（负 = topos 差）：2K proxy −1.809 / LT −3.042；4K proxy −2.068 / LT −2.494 / standard −3.863 dB（`d9552407` prores_interp_deltas，本次复核）。
- 444 域 2K：topos 4444 ~305–308 Mbps/50.7–51.0 dB vs ProRes 4444 290.4/56.8 dB（差 ~5.8 dB）。
- 2K hq：topos 191.25 Mbps/65.46 dB vs ProRes HQ 106.47/65.89——质量接近但码率 1.8×。
- 引用规则：Δ 依赖 ProRes RD 曲线插值（HQ 档越界输出 null）；两目录与 cbf6b308 数据高度同源，引用择一；素材 .mov 不在目录内无法核对生成条件。
- 归档去向：G5 边界注（与 Q01 配对数据口径不同、不可互相校准）+ G3。

### 2.3 矩阵群（77b91b28 / cbf6b308 / a986c3d9 / c0f7068d / 8efc9a50）

- **跨编码器矩阵**（cbf6b308 clean 黑苹果 / 77b91b28 i9-9900K）：编码 topos 0.136–0.238×ProRes；解码 2.0–2.6×ProRes（更慢）；444 域 vs DNxHR 367.0 Mbps/60.20 dB 显著领先 topos。
- **topos 自身矩阵**（a986c3d9 / c0f7068d）：a986c3d9 与 77b91b28 的 topos 行质量/码率逐位一致（tier 参数冻结），但 enc/dec 计时系统性更高（构建 + PyAV 版本双重差异）——"质量可比、速度不可比"的典型样本。
- **残片**（8efc9a50）：单行孤点，疑矩阵中断只落最后一 case；与 a986c3d9 4K extreme 差 ~4% 属不同 commit 构建。
- 归档去向：G3 codec 主表。

---

## 3. davinci_context 归档结论

出处：`bench_out/davinci_context/20260902_davinci_4k_export_context.txt`（2026-09-02，i9-9900K，纯文本笔记，无 manifest/JSON）。

| 项 | 数值 |
|---|---|
| 达芬奇用户实测（同任务） | ProRes 422 导出 ~100 fps、DNxHR ~75 fps |
| 本机 ffmpeg prores_ks（含解码+缩放） | ~14 fps |
| topos 编码内核（预放大）t16 | proxy/standard/hq = 18.8/16.8/15.5 fps |
| 解码+swscale 放大环节（串行） | 32.6 fps（纯软件栈瓶颈） |
| topos 整管线串行 → 流水线化 | 10.5 → 13.4 fps（+28%）；t16 比 t8 +8% |

**实测**：上表数字（文件原文）。**归因（文件作者论证，属推测性结论）**：达芬奇 7× 优势 = 硬件解码（不占 CPU 核）+ GPU 缩放/格式转换 + 自研 SIMD 编码器（公开测试为 prores_ks 的 5–10×）+ 全图流水线；同为纯软件栈时 topos 内核（15.5–18.8）≥ ffmpeg 软件 ProRes（~14）。
**引用红线**（数据卡组 2 + HISTORICAL）：非受控基准、含用户侧实测数字，只作差距归因叙事，**不得当性能数据引用**；文件自身提出的"可立即吃的两个收益"（导出流水线化 +28%、t16 +8%）后来在 m11/G2 系列兑现，可作演进叙事衔接点。

---

## 4. 根级媒体清单：cpu_real.mov / gpu_real.mov

出处：`bench_out/cpu_real.mov.manifest.json` / `gpu_real.mov.manifest.json`（created_at 2026-09-15T05:15:08/05:15:04Z，相差 4 秒；本次复核 manifest 字段）。

| 项 | cpu_real.mov | gpu_real.mov |
|---|---|---|
| 体积 | 69,841,258 B（69.8 MB） | 118,211,337 B（118.2 MB，~1.69×） |
| 输出 sha256 | 79245bd6… | 143d801a… |
| 时间线 | 同：1920×1080 / 24 fps / 48 帧 / TOPOS_HQ / 无音频 | 同左 |
| 项目/素材 | 同名项目 "gpu-verify"、同素材 2K_topos_proxy.mov | 同左 |
| 资产 sha256 | null（"延后后台生成"警告） | 4d348e8c…（已生成） |
| manifest 类型 | topos.export_manifest.v1（导出口径，**无计时字段**） | 同左 |

**归类结论**：
- **实测**：两次导出输出比特流不同（体积差 ~1.69× 即 GPU/CPU 渲染路径差异的观察点）；cpu 侧资产 sha 缺失是交付 manifest 完整性的一次真实告警样本。
- **归类**：媒体样例（大文件，禁止读入内容分析），配套性能叙事在 `macos_gpu_export_20260915/`（G2）与 ai_upgrade GPU 链（G6 §2.5）；本清单只锚定字节与 manifest 状态。
- **推测**：体积差异方向（gpu 更大）与渲染路径像素处理差异有关，未做像素对拍前不定论。

---

## 5. 附注：benchmarks/timeline_occlusion 归类

按计划 G7 附带归类（出处 `benchmarks/timeline_occlusion_bench_result.json` / `_4k.json`，数据卡组 5）：

- 2K 版：仅 B0 单轨组，cull_on 70.14 vs off 68.29 fps（speedup 1.027，culled_ratio=0）——**剔除机制未激活，"加速"为噪声，该文件无有效 A/B 组**。
- 4K 版：B1 双轨全遮挡 cull_on 11.53 vs off 3.14 fps（speedup 3.67×、帧请求 140→70 减半）为最强信号；B0/B2 符合预期（无剔除/剔除失效）。runs=2 无 CI，表述为"观察值"。
- 归类：UI/时间线渲染域（遮挡剔除），绝对 fps 与导出口径不同（渲染层帧请求，非编码导出）。
- **待重测注记（BENCH-0015，2026-09-29 追注）**：上述 runs=1/2 无 CI 与 2K 无有效剔除组的缺陷已在脚本侧修复（`tests/benchmark/benchmark_timeline_occlusion.py` 新增成对 bootstrap `speedup_ci95`、`occlusion_activated` 断言与 `--require-cull` 门禁；详见 native-threading.md §3 注记）。两版均须 runs>=3 且至少含一个全遮挡组（B1/B3）重测后，本节旧 JSON 才可被带 CI 的新数据取代。

---

## 6. 实测结论与推测分列

**实测**
1. §1 归类总表全部判定与出处；17 = 8 AB + 9 非 AB。
2. §2 三对"同源目录对"的一致性事实（质量逐位复现、tier 参数冻结、数据同源）。
3. §3 davinci_context 表中数字与 §4 媒体清单字段。

**推测 / 待验证**
1. 8efc9a50 单行成因为"矩阵中断只落最后一 case"（目录形态推断）。
2. 机器迁移推断：77b91b28/b0907366 出现真实 CPU 型号（i9-9900K）暗示换基准机或解除 CPUID 屏蔽（数据卡组 1 观察）。
3. davinci 归因四要素的定量拆分（无受控实验支撑）。
4. cpu_real/gpu_real 体积差的具体像素级来源（未对拍）。

---

## 7. 对交叉汇总（P2 ATLAS）的输入建议

1. 哈希目录应作为"P0-04 前史"单独分区：标注 commit 日期、机器（黑苹果/i9-9900K）、是否 clean、dylib 指向的仓库（topos / topos_1 / 本仓）。
2. 可提炼的长期锚点候选：a986c3d9/77b91b28 的 topos 档位质量行（跨机构逐位一致，可作为质量回归基线）；d9552407 的 Δ 插值表（口径注记必须随行）。
3. 必须防误引的三类：round2（UAF 失效）、9a8bfd99 vs 058d29da（未锚定翻盘）、2K 版 timeline_occlusion（无有效剔除组）。
