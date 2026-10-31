# G3 主题报告：codec 码率/档位/吞吐（codec）

> 计划05 P1 基准分析，2026-09-28。覆盖 `bench_out/` 下 `codec_bench`（含 2026-09-03 黑苹果主报告 + 六档矩阵 + 2026-09-16 M1 christmas）、`real_codec_bitrate_20260907`、`topos_codec_stages`。数据卡片见同目录 `cards-group2/4/5.md`；对照决策文档 `docs/codec/TOPOS_F01基线_2026-09-27.md`、`TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md`、`TOPOS编码全面检查与优化计划_2026-09-27.md`。
>
> **有效性总纲**（`bench_out/HISTORICAL.md` P2-08 + cards-group2 对 codec_bench 的专门标注）：三目录均为历史观察（dirty/混合口径/无统一 manifest）；三批数据机器口径各不相同（黑苹果 24 逻辑核 → M1 MacBook Air），**严禁互比绝对值**；实测（M）与推测/推断（P）分开标注。画质对拍（PSNR/SSIM Δ）数据不在本组三目录，属画质主题（cards-group1/2/5），本文仅在分层结论处作交叉引用并注明。

---

## 1. 档位码率阶梯与码率贴靶精度

### 1.1 真实素材六档码率 vs ProRes（real_codec_bitrate_20260907，M）

1920×1080/25fps/96 帧；422 档用 2K_prores422HQ 解码帧，444 档用 4K_prores4444 Lanczos 下变换；每行两编码器同一组帧；码率按最终 MOV 文件实占（含容器开销）。出处：`bench_out/real_codec_bitrate_20260907/report.md` 主表 + `result.json`（proxy/lt 行已逐字段核对）。

| 对应规格 | 像素格式 | TOPOS Mbps | ProRes Mbps | TOPOS/ProRes | TOPOS bpp（目标 bpp） | TOPOS QP 范围 |
|---|---|---:|---:|---:|---:|---:|
| Proxy | 422 10-bit | 48.14 | 12.69 | **3.79×** | 0.9286（0.60509） | 63–63 |
| LT | 422 10-bit | 59.55 | 40.92 | 1.46× | 1.1487（1.23239） | 60–63 |
| Standard | 422 10-bit | 92.17 | 60.62 | 1.52× | 1.7779（—） | 57–59 |
| HQ | 422 10-bit | 136.84 | 106.47 | 1.29× | 2.6397（—） | 54–56 |
| Pro444 | 444 TOPOS 10-bit / ProRes 12-bit 输出 | 298.76 | 290.44 | 1.03× | 5.7631（4.1106） | 63–63 |
| Extreme | 444 12-bit | 584.61 | 428.80 | 1.36× | 11.2772（6.7630） | 63–63 |

**QP=63 地板超支**（M，report.md 结论段 + result.json `topos_qp_min/max`/`target_bpp`）：Proxy、Pro444、Extreme 三档 qp 全帧钉死 63，实际 bpp 仍高于档位目标——0.9286/0.6051=1.53×、5.7631/4.1106=1.40×、11.2772/6.7630=1.67×（P，超支倍数由两列实测相除）。report.md 原始结论："不是码率配置小误差，而是当前帧内 VLC 内核在这些规格上的压缩能力上限"。

**排除项**：Pro444 行 ProRes 输出为 yuv444p12le（12-bit）vs TOPOS 10-bit，位深不对齐，不可作严格同质量比较（report.md 自注 + result.json `prores_probe`）。

> **验收线判定（w6-g8 注记，2026-09-29）：不满足（门禁脚本已拦截）。**
> `tools/check_codec_budget.py`（BENCH-0004 交付物）读
> `bench_out/real_codec_bitrate_20260907/result.json` 按档判定，实测输出：
> 六档体积帽全部超限（proxy 3.79× / lt 1.46× / standard 1.52× / hq 1.29× /
> pro444 1.03× / extreme 1.36×，均 > 98% 帽）+ QP63 地板三档超支
> （proxy 1.53× / pro444 1.40× / extreme 1.67×），共 9 项违规，exit 1。
> 对照决策文档 `TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md` §1
> （V10-422 验收线"98% 帽须同时验证"）/§6（最终判定规则）：验收线由
> "未判定"落为**不满足**，与本报告 §3 对照表第 2 行一致。
>
> 调参方向清单（**均需产品决策，本注记不改编码器默认参数**）：
> 1. **QP 下限放开**：proxy/pro444/extreme 三档 qp 全帧钉死 63，地板是
>    帧内 VLC 内核压缩能力上限而非码控配置问题（report.md 结论段；
>    决策文档 §1"现有 ProRes 4444 母版本身有损"）——须评估放开量化
>    步长上限（qp>63 或更粗量化核）对画质/解码兼容的影响后单独立项；
> 2. **两档体积帽重议**：444 档已收敛到 1.03×（距 98% 帽仅差 ~5 个点），
>    proxy 档 3.79× 差距最大——轻档（proxy）与重档（extreme）的 98% 帽
>    是否维持、或按"同码率非劣"单独设线，属决策文档 §1 验收线本身的
>    重议范围（§7"发布验收未判定"）；
> 3. **调档前置条件**：任何 tier target_bpp 调整须先按 Q04 扫 ≥5 质量点
>    RD 曲线（决策文档 §2/Q04"不得凭单素材重标定全档"），中档
>    （lt/standard/hq，qp 54–59 未触地板）贴靶可行，问题集中在地板三档。
> 重跑门禁：`.venv/bin/python tools/check_codec_budget.py`（调参后置为
> exit 0 即视为体积帽/地板预算两线达标；单测
> `tests/benchmark/test_check_codec_budget_aud6.py`）。

### 1.2 码率贴靶精度的世代演进（M，跨目录时间线）

| 时点 | 口径 | bytes/target 精度 | 出处 |
|---|---|---|---|
| 08-31（m8 世代） | sized 目标码率 | 误差 −18~−21% | cards-group3 m8 卡（gen_4k.json） |
| 09-02 strict 档 | sized+strict | 2K 中位 +15.16%（qp63 钉死，合成素材不可达）；4K −4.77% | `m10_encode_probe/m107c_strict/`（G2 目录交叉引用） |
| 09-02 阶段1 RC 后 | 帧间反馈单遍 | −4.9% 系统性欠靶 → **±0.5%**，同码率 PSNR +0.3~0.55 dB | `m11_stage1/20260902_stage1_rc_singlepass.txt`（G2 目录交叉引用） |
| 09-07 本组 | encode_sized 六档 | proxy/pro444/extreme 触 QP63 地板超支 1.40–1.67× | 本节 1.1 |
| ≤09-22（reexport） | 档位矩阵 | bpp ratio **0.985–1.003**（全部 14 格命中 ±1.5%） | `reexport/spec_speed_matrix.json`（G2 目录交叉引用；cards-group4） |
| 09-16 M1 christmas | 产品导出路径（首帧 sized + 帧间反馈） | 档位目标 +1.3% ~ −6.9% | `codec_bench/christmas_1080p_results_20260916.md` |

档位目标值本身也在重定档：proxy target_bpp 0.89（09-03 前）→ 0.6019（09-03 p4_retier，`bench_out/94b31cc8/…/p4_retier.json`，cards-group1）→ 0.64483（reexport 矩阵）→ 0.7448（09-21 qgain 码率预算实验，`bench_out/qgain/budget_2K_report.json`，cards-group4）（M，各文件实值）。注意 reexport 矩阵 qp_mean 68–82 高于 09-07 的 63 地板——QP 量纲在两代之间不可直接对齐（P，由数值推断，无文档确认换算关系）。

### 1.3 码率-画质关系的可用证据边界（P）

本组三目录**均不含画质指标**（codec_bench 与 real_codec_bitrate 只测码率/速度，topos_codec_stages 只测吞吐）。画质侧最近证据在别组目录（cards-group1/2/5，交叉引用、非本组实测）：
- 同码率 Δ（topos−ProRes 插值）：2K proxy −1.81 / LT −3.04 dB；4K proxy −2.07 / LT −2.49 / standard −3.86 dB（cards-group2 cbf6b308/d9552407）。
- 码率配平（±2%）真实素材 30 帧：topos proxy/lt/standard/hq Y-PSNR 42.55/47.53/50.55/55.10 vs ProRes 35.02/36.49/36.92/37.22 dB（cards-group5 topos_paired_q01_real_source `-match` 组）——与 §1.1 的"全档体积更大"合起来看：**当前 TOPOS 用更多比特换更高 PSNR，尚未证明"同质量更省"**（P，合成判断，正是决策文档 Q04 要解决的问题）。
- 世代损失（多代重编码）：10 代 PSNR-Y 74.63→72.69 dB（cards-group1 5bc83bef/6b3dfe9d）。

---

## 2. 分层（tier ladder）结论

### 2.1 速度阶梯：六档 vs ProRes（codec_bench 2026-09-03，黑苹果 24 逻辑核，M）

口径：96 帧 × 3 次中位数，topos 池 16 线程 / ffmpeg `-threads 16`，sized 定码率模式；参照 = ffmpeg prores_ks 同名 profile。出处：`codec_bench/report.md` 附录表（`tiers_result.json` 已抽查核对：proxy 2K encode 0.8363 s/96 帧 = 8.7 ms/帧，与 md 一致）。

编码（ms/帧，fps）：

| 档位 | 2K topos | 2K ProRes | 4K topos | 4K ProRes | 4K 加速比 |
|---|---|---|---|---|---:|
| 422 Proxy | 8.7（115） | 13.8（73） | 29.2（34） | 43.5（23） | 1.5× |
| 422 LT | 21.8（46） | 18.6（54） | 35.0（29） | 72.1（14） | 2.1× |
| 422 | 21.9（46） | 17.6（57） | 39.4（25） | 57.5（17） | 1.5× |
| 422 HQ | 23.0（44） | 16.6（60） | 40.3（25） | 67.9（15） | 1.7× |
| 4444 | 29.6（34） | 32.3（31） | 58.7（17） | 115.2（9） | 2.0× |
| 4444 XQ | 41.1（24） | 36.0（28） | 73.7（14） | 126.0（8） | 1.7× |

解码（ms/帧，fps）：12 个对比格 topos 11 格领先（1.07–1.66×），仅 2K HQ 略慢（226 vs 269 fps）；全部档位 2K ≥117 fps、4K ≥51 fps（同上）。

同日主报告（hq 档三方，`codec_bench/report.md` 结果表，M）：

| 操作 | 2K | 4K |
|---|---|---|
| 编码 Topos sized（HQ） | 14.1 ms（70.9 fps） | 45.2 ms（22.1 fps） |
| 编码 Topos fixed-qp（产品稳态） | 19.8 ms（50.4 fps） | 67.2 ms（14.9 fps） |
| 编码 ProRes 422 HQ | 17.8 ms（56.3 fps） | 81.9 ms（12.2 fps） |
| 编码 DNxHR HQX | 17.3 ms（57.7 fps） | 73.1 ms（13.7 fps） |
| 解码 Topos | 4.8 ms（210 fps） | 13.2 ms（75.9 fps） |
| 解码 ProRes / DNxHR | 4.0（249）/ 4.5（221） | 15.0（66.6）/ 14.7（68.0） |

- **4K 编码全六档 topos 领先 1.5–2.1×，且仅 topos 接近 25fps 实时线**；2K 大体相当（topos 0.7–1.6× 区间，proxy 反超 1.6×）（M）。
- sized（含完整 qp 搜索）比 fixed-qp 单遍慢 40–49%（2K 14.1 vs 19.8 ms；P，由两行实测相除）——与 G2 报告 §1.3 m10"探测量化 82%"互为印证。
- 码率对齐偏差对 topos 偏保守：prores_ks 出码低于档位目标（proxy 0.24 vs 0.93 bpp），topos 处理更多比特仍更快或相当（report.md 自注，M/P 混合，方向性）。
- **方差警告**：同机不同时段 2K hq 两轮 14.1 vs 23.0 ms（黑苹果功耗/调度波动），4K 各档两轮偏差 <12%；轮内背靠背相对结论稳定（report.md 方差说明，M）。

### 2.2 码率阶梯（M1 真实 30s 素材，codec_bench/christmas，2026-09-16，M）

Apple M1 MacBook Air（4P+4E 无风扇）、8 切片线程、熵编码 rans2、产品导出路径（首帧 sized + 帧间反馈单遍）。出处：`codec_bench/christmas_1080p_results_20260916.md`（JSON `christmas_1080p_results_20260916.json` 已核对 schema/host/tiers）。

| 档位 | 编码典型/峰值 fps | 解码典型/峰值 fps | 实占码率 | 平均 QP | 文件大小 |
|---|---|---|---:|---:|---:|
| 422 Proxy | 171 / 185 | 86 / 89.3 | 18.3 Mbps | 77.9 | 68.6 MB |
| 422 LT | 120 / 122.8 | 70 / 80.3 | 59.3 Mbps | 62.8 | 223.0 MB |
| 422 Standard | 103 / 104.7 | 66 / 69.3 | 91.8 Mbps | 55.3 | 345.1 MB |
| 422 HQ | 84 / 88.3 | 63 / 65.8 | 151.5 Mbps | 45.2 | 569.2 MB |
| 4444 | 50 / 53.5 | 40 / 42.3 | 249.6 Mbps | 39.4 | 938.0 MB |
| 4444 XQ | 42 / 46.0 | 36 / 38.6 | 363.7 Mbps | 32.4 | 1366.7 MB |

- 六档 1080p 编码全部高于 29.97 fps 实时（最重 4444XQ ≈1.5× 实时），解码全部高于实时播放（≈1.3×）（M）。
- 码率-档位 target_bpp 吻合 +1.3% ~ −6.9%；编码确定性：4 轮全档输出逐字节同尺寸、同 qp 序列（M）。
- 注意：M1 的 qp 量纲（32–78）与 09-03 黑苹果（54–63）不同代际/构建，不可对齐（P，同 §1.2 注）。

### 2.3 阶段分解阶梯（topos_codec_stages，2026-09-19，M）

口径：Topos 源文件 96 帧 × 3 passes 取中位，16 编码线程；encode_only（已解平面→编码）/ transcode（解+编逐帧）/ decode_out（输出解码）。出处：`topos_codec_stages/20260919_155730/results.json`（2K）、`20260919_155810/results.json`（4K）。

| 档位 | 2K enc / trans / dec_out (fps) | 4K enc / trans / dec_out (fps) |
|---|---|---|
| proxy | 397.8 / 108.5 / 642.6 | 162.8 / 47.4 / 211.3 |
| hq | 171.7 / 80.6 / 217.3 | 46.3 / 27.2 / 67.3 |
| （源 decode_only） | 135.5 | 65.4 |

- 转码路径**解码主导**：2K proxy 解码 6.3–7.3 ms/帧 vs 编码 2.1–2.3 ms/帧；4K hq 解码 14.9–16.8 vs 编码 20.4–25.1 ms/帧（M，transcode 分解字段）。
- 4K hq encode_only 46.3 fps ≈ 实时线以上，但 transcode 27.2 fps——纯编码能力与端到端转码差距约 1.7×（P，由两行实测相除）。
- 该目录源路径指向旧仓 `topos_1/`、无 manifest/dylib SHA，3 passes 中位但无 CI（cards-group5，历史观察级）。

### 2.4 分层画像小结（P，综合三目录 + 交叉引用）

档位阶梯呈三层结构：
1. **轻档（netproxy/proxy）**：编码 163–696 fps、体积最小（export_baseline 中 topos_proxy 输出 18–37 MB vs ProRes 414–437 MB，G2 交叉引用），但受 QP63 地板约束码率超支 1.53×（§1.1）——"最小档不省"的例外档。
2. **中档（lt/standard/hq）**：码率贴靶最好（qp 54–59 未触地板）、4K 编码 1.5–2.1× ProRes、解码 5–14× ProRes（reexport 矩阵）；画质侧同码率 Δ −1.4~−3.9 dB（交叉引用）。
3. **重档（4444/4444xq）**：4K 33–46 fps 编码（M1 1080p 42–53 fps），4444 档体积比 ProRes 收敛到 1.03×（§1.1），但 4444xq 仍 1.36× 且 qp63 超支 1.67×。

---

## 3. 对照 docs/codec 决策文档

| 决策要点（出处） | 本组数据对照 | 判定 |
|---|---|---|
| F01 基线：4K 编码 t16 HQ/4444/XQ 15.87/22.78/29.67 ms/帧（`TOPOS_F01基线_2026-09-27.md` §N09，即 e1_encode/44784357 数据） | 与 G2 报告 §1.1 表逐值一致（同源探针） | 一致；F01 自我限定"热点筛查口径，不构成产品性能宣称"与本报告口径声明吻合 |
| 全格式计划 V10-422 验收线："同码率非劣 + 默认档容量 ≤ 对应 ProRes 的 98% 帽须同时验证"（`TOPOS全格式ProRes对标…` §1/§6） | §1.1：全六档 TOPOS/ProRes 体积比 1.03–3.79×，全部 >1 | **98% 体积帽当前数据不满足**；与该计划第 7 节"发布验收未判定"一致 |
| Q04："默认档位更小 ≠ 同质量更省"，须扫 ≥5 质量点 RD 曲线（同上） | 本组三目录无 RD 曲线、无画质指标（§1.3）；码率配平证据仅在 paired_q01（别组） | Q04 所需证据**不在本组**，码率侧本组只能给出阶梯与贴靶精度 |
| 公平视角 A"实际视频载荷码率匹配 ±2%"（同上 §2） | reexport 矩阵 bpp ratio 0.985–1.003（RC 后世代，交叉引用）；但 proxy/pro444/extreme 三档在真实素材 QP63 地板不可达 | 视角 A 在多数档可行、地板三档应记"未判定"（计划原文允许） |
| QP63 地板=帧内 VLC 压缩上限（real_codec_bitrate report.md 结论；全格式计划 §1"现有 ProRes 4444 母版本身有损，不能充当无损真值"） | §1.1 三档超支 1.40–1.67× | 地板档是压缩能力问题非配置问题；任何"调 target_bpp 即可"的预期不成立（P） |
| F03："4K HQ 编码本体是主要时间项"（`TOPOS编码全面检查…` §C/F 表） | m10 sized 探测量化 82%（G2 交叉引用）；codec_bench sized vs fixed-qp 慢 40–49% | 一致；稳态产品路径（单遍+反馈）已绕开 sized 最坏情况（christmas 口径） |
| P03："更快须同码率/同画质条件下声明"（全格式计划 §6/最终判定规则） | codec_bench 4K 1.5–2.1× 领先建立在 topos 出码**更高**的前提下（report.md 自注） | 速度声明必须带码率条件；本组数据不足以单独支撑"更快"发布宣称 |

> **Q04 证据路径注记（BENCH-0013，2026-09-29 追注）**：上表 Q04 行的"证据不在本组"状态已有官方补采通道——`tools/bench_q01_rd_curve.py` 在冻结语料上扫 ≥5 质量点（TOPOS 固定 QP + prores_ks `bits_per_mb` 双臂，<5 点拒绝运行），自动产出决策文档要求的码率匹配与画质匹配两表及预注册 Q04 判定（详见 quality.md §3.1 追注）。98% 体积帽（§1.1）与 Q04"同质量更省"是两条独立验收线：前者已判不满足（BENCH-0004 处置中），后者待本脚本在 clean 构建上采集后方可判定。

---

## 4. 有效性与排除清单

| 数据 | 处置 | 原因 |
|---|---|---|
| `codec_bench` 三批（09-03 主报告 / 09-03 六档 / 09-16 M1） | 各自内部可用，**批次间绝对值严禁横比** | HISTORICAL.md 明标"dirty 工作树、混合口径 harness、历史观察"；黑苹果→M1 机器不同（cards-group2） |
| `codec_bench/report.md` 2K hq 23.0 ms 那一轮 | 引用时须附方差注 | 运行间方差 14.1 vs 23.0 ms（同机不同时段） |
| `real_codec_bitrate_20260907` Pro444 行 | 不作严格同质量比较 | 位深不对齐（ProRes 12-bit vs TOPOS 10-bit）；构建库路径指向 `topos-color02` 工作区、无 SHA 锚定、96 帧单次无 CI |
| `topos_codec_stages` | 历史观察级 | 源路径旧仓 topos_1、无 manifest、3 passes 无 CI |
| reexport 矩阵 QP 值与 09-03/09-07 系列 | 不可对齐 | QP 量纲跨代不同（§1.2 注，P） |
| m8 世代 sized 误差 −18~−21% | 不与 m11 后 ±0.5% 混用 | RC 已重写（cards-group3 m8 卡自注） |

## 5. 结论

1. **码率阶梯（M）**：真实素材 1080p25 六档 48.1→584.6 Mbps；全档体积大于对应 ProRes（1.03–3.79×），444 档最接近（1.03×）、proxy 最差（3.79×）。98% 体积帽验收线当前不满足，与决策文档"未判定"状态互证。
2. **贴靶精度（M）**：RC 演进使 bytes/target 从 −4.9% 欠靶修到 ±0.5%（m11）、后世代档位矩阵 bpp 命中 0.985–1.003；但 proxy/pro444/extreme 的 QP63 地板是内核压缩上限（超支 1.40–1.67×），非码控问题。
3. **速度分层（M）**：4K 编码全六档 1.5–2.1× ProRes（黑苹果口径）、M1 1080p 六档全超实时（42–185 fps）；解码 12 格 11 格领先。转码路径解码主导（2K proxy 解码 6.3–7.3 vs 编码 2.1–2.3 ms/帧）。
4. **码率-画质关系（P）**：本组三目录无画质指标；配平码率下 topos PSNR 大幅领先而体积更大的合成判断，需 Q01 扩样 + RD 曲线（决策 Q04）落地后方可宣称。
