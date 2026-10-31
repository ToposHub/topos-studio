# G4 · native 域与线程（native-threading）

计划05 P1 主题分析，2026-09-28。范围：`bench_out/topos_native_domain_matrix`、`topos_native_domain_float_zero_audit`、`topos_native_domain_smoke`、`m103_slice_rows`、`m104_pipeline`、`m1063_kernel`、`topos_perf_thread_refine_3rounds.json`、`benchmarks/timeline_occlusion*`，并与 4K XQ 线程扩展试点（git commit `0724035c`，2026-09-28 09:04 +0800「Record 4K XQ thread scaling pilot and remaining performance gate」）对照。输入卡片：`cards-group3.md`、`cards-group5.md`；原始数据均已复核到文件级。

有效性总纲（`bench_out/HISTORICAL.md`）：除 `058d29da/` 结果树外均为历史观察；相对结论（"快/慢 x%"）须同 clean HEAD、同 dylib SHA、交替 A/B 中位数 + bootstrap 95% CI 且区间不跨 1.0。**本文全部线程曲线均未达该门槛**（dirty 树 / 无 CI / 跨构建），一律按"趋势观察"表述；标注【实测】= 原始数据中直接读出的数，【推断】= 人为归因或外推（含原报告作者的归因）。

---

## 1. 线程扩展曲线

### 1.1 4K XQ 编码分段试点（i9-9900K 8C/16T，2026-09-27/28）

口径：E1 分段 = ToposVideoEncoder 平面准备 → native 编码 → mux，排除源解码/GPU/应用调度；16 测量帧 + 4 预热，轮内线程顺序交错。5 轮扫描与 3 轮细化是**两个不同构建**（库 SHA 不同），计划文档明确禁止两组相减当优化收益（`docs/codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md` P01/P02 节）。

**5 轮扫描 1/8/16 线程**【实测】。出处：`docs/codec/evidence/TOPOS_2026-09-27_local_pilot/performance_4k_xq_thread_sweep_5rounds.json`（底层数据 `bench_out/e1_encode/c1245ec6/d5fb9ad5/c9fb38a7/20260927T16*/result.json`，git_head `c1245ec6` dirty，dylib `d5fb9ad5…`，ABI 2，AVX2）：

| 线程 | 5 轮 wall 中位数 (ms/帧) | 折算 fps | 轮间范围 | 逐帧 p95 中位数 (ms) | native 段中位 (ms) | mux 段中位 (ms) | 峰值 RSS 中位 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 224.12 | 4.46 | 200.73–225.76 | 260.11 | 217.87 | 4.04 | 2.18 GiB |
| 8 | 44.12 | 22.67 | 44.04–44.41 | 57.22 | 37.34 | 4.65 | 2.22 GiB |
| 16 | 32.66 | 30.62 | 29.96–34.49 | 44.68 | 26.42 | 4.43 | 2.21 GiB |

（中位数由 5 轮原始值复算，与计划文档 P01 表一致；p95 轮间范围 1/8/16 分别为 229.54–262.59 / 55.15–60.56 / 35.40–47.95。RSS 含约 20 帧源预载，非应用导出峰值。）

**3 轮细化 8/12/16 线程**【实测】。出处：`bench_out/topos_perf_thread_refine_3rounds.json`（行级引用 `bench_out/e1_encode/1d30609f/7b0cc4d4/4232ea12/20260928T01*/result.json`，git_head `1d30609f` dirty，dylib `7b0cc4d4…` 独立 perf_current 构建）：

| 线程 | 3 轮 wall 中位数 (ms/帧) | 折算 fps | 逐帧 p95 中位数 (ms) | native 段中位 (ms) | mux 段范围 (ms) | 峰值 RSS 范围 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 39.43 | 25.36 | 48.23 | 33.13 | 4.36–4.69 | 2.35–2.40 GB |
| 12 | 34.37 | 29.09 | 43.49 | 28.80 | 3.94–6.28 | 2.36–2.37 GB |
| 16 | 33.86 | 29.53 | 41.63 | 27.33 | 4.51–4.61 | 2.40–2.44 GB |

**饱和点（趋势观察）**：5 轮口径 1→8 加速 5.08×（并行效率 63.5%）、1→16 加速 6.86×（效率 42.9%）、8→16 再 +35%；3 轮口径 8→12 +14.7%、**12→16 仅 +1.5%——4K XQ 该分段在 i9-9900K 上约 12 线程即近饱和**。mux 段恒定 3.9–6.3 ms/帧，随线程数不变【实测】。两构建的 8 线程绝对值差 44.12 vs 39.43 ms，不可跨构建比较。

### 1.2 历史线程扩展证据（与试点同机 i9-9900K 或元数据不完整机器）

**(a) 2K 解码线程曲线（内核口径，sr32 hq 码流）**【实测】。出处：`bench_out/m1063_kernel/20260902_slice_rows_default_evidence.txt`：

| 线程 | 1 | 2 | 4 | 8 | 12 | 16 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fps | 60.96 | 121.11 | 218.95 | **257.11** | 246.35 | 245.72 |

t8 峰值后 t12/t16 反降 4.2–4.4%——**t8 即饱和且过订阅轻微负收益**。作者归因（【推断】，同文件 + `profile_dec_2k_product.txt`）：sr32 下 34 带/8 线程 = 4.25 带/线程，带内 DC/熵链路串行导致 barrier 尾部失衡，t8 worker 有效利用率仅 ~56%；根因是调度粒度而非线程数。改为 sr16 后同机 t8 解码 382.25 fps（+48%），sr8 379.41 无进一步收益；2K sr16 相对 sr32 编码 +21%/解码 +32%、PSNR −0.2 dB@67.7 档；4K 仅 enc +9%/dec +10%（全代价矩阵，同文件）。

**(b) 4K 编码线程扫描（commit 消息口径）**【实测】。出处：git `0b6322d8`（2026-09-01）提交信息：「4K 编码线程扩展扫描（clang-cl，t1/t2/t4/t8/t16 = 1.22/2.17/3.36/4.16/4.19 fps，16 线程仅 3.4x 加速）」——t8→t16 +0.7%，**t8 饱和**，16 线程并行效率 3.43/16 ≈ 21%。

**(c) m103_slice_rows（24 逻辑核 Intel，CPU 串被掩码为 "Genuine Intel 0000"，具体型号未记录；topos_commit `0b6322d8`）**【实测】。出处：`bench_out/m103_slice_rows/20260901_233648_4k/result.json`、`20260901_234156_2k/result.json`：

| 分辨率/sr | enc t8 | enc t16 | dec t8 | dec t16 |
| --- | ---: | ---: | ---: | ---: |
| 2K sr32 | 14.42 | 14.90 | 193.9 | 190.2 |
| 2K sr16 | 16.21 | 17.45 | 279.6 | 260.0 |
| 2K sr8 | 17.91 | 18.88 | 262.7 | 270.4 |
| 4K sr32 | 4.30 | 4.38 | 72.7 | 71.4 |
| 4K sr16 | 4.59 | 4.59 | 76.5 | 75.2 |
| 4K sr8 | 5.00 | 5.00 | 76.8 | 74.9 |

t16 相对 t8 的差异在噪声级（≤ +5%，解码侧多为持平或略负）——该机/该代代码下 8→16 无收益，与 (a)(b) 的 t8 饱和一致。码流体积随粒度变化：4K |Δ| ≤ 0.088%、2K sr16 +0.40%、**2K sr8 −1.20%**（`bytes_delta_vs_base` 字段；注：cards-group3 概括为"≤0.4%"未覆盖 2K sr8 的 −1.20%，以本表原始值为准）。2K 粒度收益（sr32→sr16 编码 +12%、解码 +44%）远大于 4K（+7%/+5%）。

**(d) m104_pipeline（同 24 逻辑核机，topos_commit `e291c87b`）**【实测】。出处：`bench_out/m104_pipeline/20260902_*/result.json`（6 个子目录）。编码流水线化（serial vs pipeline worker2/3），48 帧，`bitexact_vs_serial` 全 true：

| 子目录 | 分辨率 | serial | w2 | w3 |
| --- | --- | ---: | ---: | ---: |
| 004550_2k / 004944_2k | 2K t8 | 63.4 / 63.7 | 72.0 / 71.3 | 70.9 / 72.0 |
| 000717_4k | 4K t8 | 19.2 | 19.3 | 19.4 |
| 004958_4k | 4K t8 / t16 | 17.7 / 17.5 | 18.5 / 18.2 | 18.4 / 18.1 |
| 010250_2k（代码再迭代后） | 2K t8 | 114.0 | 140.7 | **175.5 (+54%)** |
| 010301_4k（代码再迭代后） | 4K t8 | 31.3 | **38.1 (+22%)** | — |

t16 与 t8 在 4K 流水线口径同样持平（17.5 vs 17.7 serial）。同目录同 commit 前后两轮绝对 fps 差近 2×（63→114），窗口内代码快速演进，引用必须点名子目录。

**(e) 重载荷纯内核 t16 惩罚**【实测】。出处：`bench_out/m11_stage2b/20260902_stage3a_mux_dead_crc.txt` 附录（qp20 固定、纯内核、成对 10 对）：t8 18.29 fps vs t16 14.56 fps，**t16/t8 = 0.799（t16 慢 20.1%）**，逐对比值 [0.707–0.826] 全负。归因【推断，同文件】：HT 同核争抢 entropy/fill；产品导出维持 `min(8, cpu)` 线程策略。注意口径矛盾解除记录：3c 的「t16 15.12ms」为 qp55 轻载荷，与本 A/B 不可比。

**(f) 16 物理核机上 8→16 为正收益**【实测】。出处：`bench_out/topos_real_bench_2026-09-13/threads_8v16.json`（Mac Pro i9-12900T 16C/24T，4K hq rate、sr16、rans2、120 帧、交替序 [8,16,16,8,8,16]）：

| 线程 | 编码 fps（3 次/中位） | 解码 fps（3 次/中位） | 码率 |
| ---: | ---: | ---: | ---: |
| 8 | 29.88 / 33.61 / 33.73 → 33.61 | 38.28 / 40.51 / 40.64 → 40.51 | 531.43 Mb/s 恒定 |
| 16 | 49.92 / 51.07 / 53.90 → 51.07 | 68.75 / 68.80 / 71.96 → 68.80 | 531.43 Mb/s 恒定 |

16/8 编码 +52%、解码 +70%，qp_mean 61.542 逐位恒定——**线程扩展结论强依赖物理核数**：8C16T 机饱和于 ≤8–12，16C24T 机 16 线程仍有大幅收益。

### 1.3 饱和点汇总

| 工作负载 | 机器 | 饱和点 | 出处 |
| --- | --- | --- | --- |
| 4K XQ E1 分段（prep+enc+mux） | i9-9900K 8C16T | ~12 线程（12→16 仅 +1.5%） | `topos_perf_thread_refine_3rounds.json` |
| 4K XQ 同上（另一构建，1/8/16） | i9-9900K 8C16T | 未及 8 前已弱化（8→16 +35% 后仍升，1→16 效率 43%） | `performance_4k_xq_thread_sweep_5rounds.json` |
| 4K 编码内核（clang-cl 扫描） | 未记录 | t8（t16 +0.7%） | git `0b6322d8` 提交信息 |
| 2K 解码内核 sr32 | i9-9900K | t8（t12/t16 −4%） | `m1063_kernel/20260902_slice_rows_default_evidence.txt` |
| 2K/4K 编码+解码（m103） | 24 逻辑核掩码 Intel | ≤ t8（t16 噪声级） | `m103_slice_rows/*/result.json` |
| 2K qp20 重载荷纯内核 | i9-9900K | t8 为最优点（t16 −20%） | `m11_stage2b/...mux_dead_crc.txt` 附录 |
| 4K hq 产品路径 | i9-12900T 16C24T | 16 线程未饱和（+52%/+70%） | `threads_8v16.json` |

---

## 2. native 域矩阵覆盖与 float zero audit

### 2.1 topos_native_domain_matrix（合成 128×128，2026-09-27 15:30–15:37 UTC）

覆盖范围【实测】。出处：`bench_out/topos_native_domain_matrix/<kind>/native_domains_<kind>.json`。toos CLI SHA：raw12/raw16/float16 = `06508c5c…`，float16-finite = `4b8e5366…`（15:37 复测；注：cards-group5 将四域统记为 06508c5c，以各 JSON 自带字段为准）。每份自带源 SHA、CLI SHA、测量脚本 SHA；状态自标 `synthetic_or_input_domain_measurement_no_release_verdict`；**全部 20 档 `bit_exact: false`、每档单次、无任何计时字段（本目录对线程主题零贡献）**。

| 域 | 档数 | 压缩比（源/编码） | 质量范围 |
| --- | ---: | ---: | --- |
| raw12（四相位 12-bit CFA） | 6 | raw12-2 1.55× → raw12-16 4.14× | PSNR_min 108.4 dB / max_abs 1 code → 56.7 dB / 30 code |
| raw16（四相位 16-bit CFA） | 6 | 1.48× → 5.85× | 96.8 dB / 3 code → 50.8 dB / 849 code |
| float16（三通道 half，含特殊值） | 4 | ultra 2.84× → low 10.14× | max_half_ulp 2 → 1102 |
| float16-finite（有限值 + 1 个负零） | 4 | 2.84× → 10.14× | 与 float16 编码字节逐档完全相同（34,574 / 29,594 / 15,711 / 9,696 B） |

配套真实素材对照（相邻目录，一并参考）：`bench_out/topos_raw_real_cfa_probe/raw16_six_tiers/native_domains_raw16.json`——真实 ProRes RAW 裁切 2048×2048 RGGB raw16 六档：raw16-2 2.54×/96.9 dB → raw16-16 **102.4×**/60.8 dB，真实低熵内容压缩比远超合成（5.85×），合成绝对值不可外推。覆盖缺口：无真实 RAW12 矩阵、无真实 EXR/float 素材、单次无重复、无预注册阈值。

`topos_native_domain_smoke`（2026-09-27 23:29–23:37）：4 个合成源（raw12.raw / raw16.raw / float16.raw / float16-finite.raw，float16-finite.raw 98,304 B = 128×128×3×2 B）+ raw12-4 档往返一组（`raw12-4.dec.raw.json` 头部确认 width/height 128、bit_depth 12、pixel_format 3、channels R,Gr,Gb,B、uint16 LE planar、`concealed_slices: 0`）。仅冒烟素材与单点往返，无数值结论（注：cards-group5 记"5 个合成源"，目录实为 4 源 + 1 组往返产物）。

### 2.2 topos_native_domain_float_zero_audit（2026-09-28 00:42 UTC）

测试对象：finite 源（`docs/codec/evidence/TOPOS_2026-09-27_local_pilot/float16-finite.raw`，含 1 个负零）过 float16 四档编解码后，对 ±0、零↔非零跃迁、特殊类（Inf/NaN）变化逐通道（G/B/R）核查。CLI 已换为 `333b7df3…`（新于 matrix 的 06508c5c/4b8e5366）；压缩比与编码字节和 matrix finite 版逐档一致（可交叉验证码流未变）【实测，出处 `bench_out/topos_native_domain_float_zero_audit/native_domains_float16.json`】：

| 档 | 压缩比 | max_half_ulp | p99 half_ulp | zero_sign_changes | source_zero_to_nonzero | negative_zero_to_nonzero | source_nonzero_to_zero | special_class_changes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| float-ultra | 2.843 | 2 | 1.0 | **1（G 通道）** | 0 | 0 | 0 | 0 |
| float-high | 3.322 | 5 | 3.0 | 0 | **1** | **1** | 0 | 0 |
| float-medium | 6.257 | 199 | 92.0 | 0 | **1** | **1** | 0 | 0 |
| float-low | 10.139 | 1102 | 496.17 | 0 | **1** | **1** | 0 | 0 |

**Audit 结论**：① 源中唯一的负零在 ultra 档变正零（zero_sign_change=1），在 high/medium/low 档变为非零——**四个有损档均不承诺带符号零 bit-exact**；② 全档 `source_nonzero_to_zero=0`（无非零塌缩为零）、`special_class_changes=0`（无 Inf/NaN 类变化、无新负零产生）；③ 全档 `bit_exact=false`，ULP 漂移随档位单调放大（2→1102）。产品侧后果（【实测】的行为记录，出处：计划文档 IRAW/IF16 行 + `evidence/TOPOS_2026-09-27_local_pilot/README.md`）：`float-*` 有损预设会把 Inf/NaN 量化为有限值，产品编码入口与 CLI 已改为**写盘前拒绝非有限值**（负向测试 `tests/native/test_topos_float_tier_nonfinite.py`、`tests/media/test_topos_half_float.py`）；显式 HALF/固定 QP 路径不受四预设语义影响。

---

## 3. timeline_occlusion 遮挡剔除（2K/4K）

口径：`tests/benchmark/benchmark_timeline_occlusion.py`（实机真实素材，每组 cull=ON vs OFF 的时间线渲染 wall 中位数 + layer 取帧请求计数；此为渲染层帧请求口径，非编码导出 fps）。出处：`benchmarks/timeline_occlusion_bench_result.json`（2K：2K_h264.mp4 `038491830c2b` + 2K_prores422HQ.mov `2d6a348afa44`，warmup 4 / measure 12 / **runs=1**）、`timeline_occlusion_bench_result_4k.json`（4K_H264.mov `c90cf62a5f99` + 4K_prores422HQ.mov `7438f4962405`，warmup 10 / measure 60 / **runs=2**）【实测】：

| 版本 | 组 | cull_on (fps) | cull_off (fps) | speedup | 取帧请求 on/off | culled_ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2K | B0 单轨 | 70.14 | 68.29 | 1.027 | 16 / 16 | 0 |
| 4K | B0 单轨 | 24.24 | 23.71 | 1.022 | 70 / 70 | 0 |
| 4K | B1 双轨全遮挡 | **11.53** | 3.14 | **3.67×** | **70 / 140** | **0.5** |
| 4K | B2 上层缩放（无整轨遮挡） | 2.12 | 2.13 | 0.994 | 140 / 140 | 0 |

解读：唯一有效信号是 4K B1——被遮轨整轨剔除使取帧请求减半、wall 中位 19.12→5.20 s；B0 两组剔除未激活（culled_ratio=0），1.02× 的"加速"为噪声；B2 的 0.994 证明上层缩放时剔除正确保持不介入（符合机制预期）。**2K 文件只有 B0 单轨组，无有效遮挡组，整体不构成 2K 剔除证据**（排除，见 §5）。runs=1/2 无 CI，B1 的 3.67× 按 HISTORICAL 规则只能表述为观察值。

> **待重测注记（BENCH-0015，2026-09-29 追注）**：`tests/benchmark/benchmark_timeline_occlusion.py` 已补齐采集纪律——`--runs N`（ON/OFF 逐轮交错）+ 每组 `speedup_ci95`（成对百分位 bootstrap，统计量 median(off)/median(on)；runs<2 为 null）+ `occlusion_activated` 剔除组生效断言（cull_on 侧 culled_ratio>0）；没有任何组激活时打 WARNING 并写 `warning_no_active_cull_group`（`--require-cull` 升级为 exit 1）。**2K 与 4K 两版均须以 runs>=3 重测**后，B1 类 speedup 才可携带 CI 引用（本机小样本自验：2K B1 speedup 3.05× CI95 [2.88, 3.20]、请求比精确 2:1——证明 2K 素材含 B1 组即可得到有效剔除证据，历史 2K 文件只是漏跑了遮挡组）。

---

## 4. 与 4K XQ 线程试点结论对照

试点的正式结论（出处：git `0724035c` 提交的三份文件——计划文档 P01/P02 节、`evidence/TOPOS_2026-09-27_local_pilot/README.md` 与两个 JSON；本报告 §1.1 即其原始数据）：

1. **P01（5 轮基线）**：16 线程分段 p95 中位 44.68 ms **仍高于 4K25 的 40 ms 帧预算**，平均 30.6 fps 不能宣称完整链路实时；产品时间线还需为源解码保留线程。带 `TOPOS_CODEC_PROFILE=1` 的单次 XQ 探针提示 native ≈26.2 ms/帧、mux ≈4.8 ms/帧、worker 累计 entropy/fill 189.8/110.7 ms/帧【实测探针值；"下一轮定位入口"为作者推断，不可相加成 wall】。
2. **P02（3 轮细化）**：12→16 只缩短分段平均墙钟约 1.5%，p95 未稳定进入帧预算；**未据此修改产品线程策略**，需在真实整链竞争（导出时解码/渲染并行）、功耗、RSS、画质/码率不变性上补测。

与历史证据的一致性对照：

- **一致的**：4K XQ 分段在 i9-9900K 上收益递减至 ~12 线程，与该机历史曲线"t8 后饱和"（§1.2 a/b/c）方向相同；试点未动产品 `min(8, cpu)` 策略，与 m11_stage2b 的 t16 重载荷惩罚（§1.2 e）相符——E1 分段含 Python prep/mux 串行成分，掩住了纯内核 t16 惩罚，不代表内核口径下 16 线程更优。
- **互补的**：m1063 证明 2K 解码"饱和"的真因是 slice 调度粒度（sr32→sr16 +48%），线程数扫描在粒度修好后才有意义；m104 证明另一条正交路径（流水线 w2/w3：2K +54%/4K +22%）在 t8 不变下拿到收益。4K XQ 试点相当于把"线程数"这一轴在 XQ 档重测，结论（12 后饱和、p95 不达标）与历史一致。
- **冲突需注明的**：`threads_8v16.json`（12900T，16 物理核）显示 8→16 编码 +52%/解码 +70%——与 9900K 结论不矛盾（物理核数不同），但意味着**任何"最优线程数"结论只对列明机器成立**，跨机器不可迁移。
- 【推断，本报告观点】：4K XQ 分段 p95 距 40 ms 预算差 ~4%（41.63 vs 40），线程轴已近枯竭（12→16 +1.5%），下一步收益更可能来自 m104 式流水线化或熵段内核（P01 的 entropy/fill 累计占比提示），而非继续加线程——此为待验证推测，非数据结论。

> **推断落地注记（BENCH-0014，2026-09-29 追注）**：上一条"收益应转向流水线/熵段"的【推断】已落成可复跑的预注册判定门 `tools/bench_thread_pivot_gate.py`——同一构建、同素材、轮内交错跑线程序列（默认 8/12/16），按 E1 分段口径（ANC-16 同边界）逐轮记录逐帧 wall p95 与 native 阶段统计（`TOPOS_CODEC_PROFILE=1`，BENCH-0010 口径）中 entropy+fill 的 worker 累计份额（分配信号，不得当 wall 归因相加），判定规则预注册：① 最高两档 wall 收益 ≥5%（ANC-16 漂移带反命题）→ `threads_still_profitable`（先吃线程轴）；② 线程饱和 + p95 超 40 ms 预算 + entropy+fill 份额 ≥30% → `pivot_justified`（转向流水线/熵段由数据成立，m104 先例 2K +54%/4K +22%）；③ 饱和 + 超预算 + 份额不足 → `pivot_target_unclear`（先重新剖析）；④ 饱和 + 预算内 → `budget_met_no_pivot`；rounds<2 → `insufficient_rounds`。试点现状形态（12→16 +1.5%、16T p95 41.63、熵段份额达标）按该门为 `pivot_justified`（单测锁定）。正式判定须 clean 构建 `--rounds >= 3`（默认 3），结果落 `bench_out/thread_pivot_gate/<head>/<dylib8>/<manifest8>/<utc>/result.json`。截至本注记日 clean 构建数据尚未采集，§4 该条仍按【推断】引用；后续任何"线程策略/流水线优先"决策应以该门的结果为准。

---

## 5. 不可比数据与排除说明

| 项 | 原因 | 处置 |
| --- | --- | --- |
| 5 轮扫描 vs 3 轮细化绝对值 | 不同构建（`c1245ec6/d5fb9ad5` vs `1d30609f/7b0cc4d4`），计划文档明令禁止相减 | 仅各自内部比值 |
| 试点 vs m103/m104/m1063/m11 | 不同 commit（dirty 树）、不同口径（E1 分段 ms/帧 vs 内核 fps 循环 vs 产品路径） | 只比"饱和点方向"，不比绝对 fps |
| m103/m104 机器 | `cpu="Genuine Intel 0000"` 掩码、logical_cpus=24，型号未记录；与 i9-9900K（16 逻辑）必不同机 | 机器身份不明，绝对值孤立引用 |
| 12900T threads_8v16 | 16C24T 与 8C16T 物理核数不同；产品路径 wall 含 Python setup | 仅作机器依赖性反例 |
| m104 前两轮 vs 后两轮 | 同目录同 commit 但代码窗口内演进（serial 63→114 fps） | 引用必须点名子目录 |
| timeline_occlusion 2K 版 | 仅有 B0 单轨组，culled_ratio=0、runs=1，剔除机制未激活 | 整体排除，不作 2K 剔除证据 |
| timeline_occlusion 4K B1 3.67× | runs=2 无 bootstrap CI | 表述为观察值 |
| native domain matrix / audit / smoke | 无计时字段；合成 128×128 单次；CLI SHA 三代（06508c5c/4b8e5366/333b7df3） | 不进入任何线程/性能结论；audit 与 matrix 仅字节级可比 |
| 全部线程曲线 | 均未达 HISTORICAL 规则 2 门槛（clean HEAD + 同 dylib + 交替 A/B + CI） | 一律"趋势观察"，禁止"快/慢 x%"式发布结论 |
| 试点 RSS | 含约 20 帧源预载的进程高水位 | 不等同应用导出峰值 |

## 6. 卡片勘误（供 P1 汇总采纳）

1. cards-group3 `m103_slice_rows` 卡"码流体积随粒度变化极小：bytes_delta_vs_base ≤ 0.4%"——原始数据 2K sr8 为 **−1.20%**（`20260901_234156_2k/result.json`），"≤0.4%"仅对 2K sr16 与 4K 成立。
2. cards-group5 `topos_native_domain_matrix` 卡记四域 toos SHA 统一为 `06508c5c`——float16-finite 子目录 JSON 自记为 `4b8e5366`。
3. cards-group5 `topos_native_domain_smoke` 卡记"5 个合成源 raw"——目录实为 4 个源 raw + 1 组 raw12-4 往返产物。

**勘误 2 扩写：toos CLI 三代 SHA 混用的完整影响面（BENCH-0016，2026-09-29 追注）**

三代 CLI 版本（matrix 主体 `06508c5c` / matrix float16-finite `4b8e5366` / audit+真实 CFA `333b7df3`）在同一批 native 域证据链中混用。逐结论拆解"哪些只靠字节一致交叉验证支撑"：

| 结论 | 依赖的 CLI 版本 | 支撑形态 | 混用风险 |
|---|---|---|---|
| audit（333b7df3）与 matrix finite（4b8e5366）"压缩比与编码字节逐档一致"（§2.2 前提） | 两个不同版本 | **仅靠字节一致交叉验证**（编码产物 sha 逐档相同）——这是两代 CLI 之间唯一的桥 | 若字节不一致，无法区分"CLI 版本演进"与"内容差异"；当前一致 → 码流未变结论成立 |
| matrix 内 raw12/raw16/float16（06508c5c）与 float16-finite（4b8e5366）"编码字节逐档完全相同"（§2.1 表） | 两个不同版本 | **仅靠字节一致交叉验证** | 同上；结论当前成立但属"事后对账"，非同版本同窗对照 |
| §2.1 各域数值本体（压缩比/PSNR/max_abs/max_half_ulp 等） | 各自单版本单次 | 无跨版本验证 | **单版本孤立值**：任何跨 CLI 世代的数值漂移都无法归因（二进制演进 vs 回归）——这正是 BENCH-0016 的判据"跨 CLI 版本的变化属二进制演进而非回归" |
| 真实 CFA 六档（333b7df3，quality.md §4 / ANC-12） | 单版本 | 无任何交叉验证（素材独占） | 孤立锚点；复测若不钉住 CLI 版本，±15% 漂移带内外的变化都不可判读 |
| audit 的行为语义结论（负零变正零、无 Inf/NaN 类变化，§2.2） | 333b7df3 单版本 | 字节一致（与 matrix finite）间接支撑"码流同源" | 语义结论绑定在该版本行为上；CLI 换代后语义可能变，须重测而非沿用 |

处置（防再发）：`tools/measure_topos_native_domains.py` 已加 `--record-cli-sha [EXPECTED_SHA]`——把 CLI SHA-256 写入 out-dir 的 `result.json`；带 EXPECTED_SHA（完整或 ≥8 位前缀）时先断言匹配再测量，不匹配直接失败（不产生档位数据）。**ANC-08~ANC-12 复测前必须先固定 CLI 版本**（用该旗标钉住），跨 CLI 版本的压缩比/误差变化按二进制演进处理、更新锚点而非报回归。
