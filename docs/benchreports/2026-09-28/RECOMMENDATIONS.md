# RECOMMENDATIONS（计划05 P4：建议台账，2026-09-28）

> 编号 BENCH-0001 起。每条 = 观察现象 + 数据证据（出处）+ 假设 + 建议动作（测量类/优化类）+ 优先级。**只建议，不实现。**
> 结构化版本见 `FINDINGS.json`（id=BENCH-xxxx、source=plan-05、type ∈ perf-opportunity/risk/process、severity、title、location、evidence、recommendation、confidence、effort、round=1、status=open），两文件条目一一对应（17 条）。
> 数据出处均为仓库根相对路径；报告文件指 `docs/benchreports/2026-09-28/` 下同名文件。
> 优先级定义：P1 = 影响发布验收或有用户可感风险；P2 = 影响决策质量/工程效率；P3 = 卫生类。

---

## BENCH-0001 首帧/冷启动延迟零数据缺口

- **观察现象**：播放主题覆盖目录内没有任何首帧/冷启动计时字段。`first_frame_repro` 是纯功能复现语料（11 个 mov + manifest，零计时）；D1 基准无冷启动字段且 `random_seeks=0`；readahead 证据链只测稳态顺序吞吐。
- **数据证据**：`bench_out/first_frame_repro/`（无计时，报告 playback.md §5）；`bench_out/d1_playback/*/…/result.json` 的 `config.random_seeks=0`；最近的零星数据在覆盖外：`bench_out/p0_runner/p0_runner_20260916_005041.json`（`cold_first_frame` n=3 样本极小）、`bench_out/p2_09/p2_09_pool_20260916_114756.json`（冷首帧 p50：h265 35.2 ms / prores 165.3 ms，会话池逐出重建口径）。
- **假设**：首帧延迟由「打开（容器探测/decoder 构建）+ 首包读取 + 首帧解码 + 首次 GPU 管线编译」串联，当前无任何一环有基线，回归将不可见。
- **建议动作（测量类）**：设计首帧基准（cold open / warm reopen / 池逐出重建三态，≥30 样本 + p50/p95），优先复用 `tests/benchmark/benchmark_runner.py` 已有的 `cold_first_frame` 相位扩样，并纳入 P0-04 manifest 树；与 `first_frame_repro` 语料打通（同素材 sha256）。
- **优先级**：P1（playback.md §8 列为最大数据缺口）。

## BENCH-0002 4K 4444xq p99 击穿 30fps 帧预算的风险

- **观察现象**：D1 解码边界下 4K 4444xq p50 达标（23.2–24.6 ms < 33.3 ms），但 p99 29.2–36.6 ms 已有轮次击穿 30fps 时间线的 33.3 ms 预算；25fps（40 ms）余量仅 9–27%。且 D1 不含 GPU 上传/呈现，端到端只会更差。
- **数据证据**：`bench_out/d1_playback/b61cabf4/9a36f61b/8fedcb41/20260915T051555356217Z/result.json` 第二轮 p99=36.638 ms（报告 playback.md §2.2）；预算口径 `docs/4k-playback-optimization.md` §3.1。
- **假设**：p99 尾部来自 slice 尾部失衡/页错误/调度抖动（D1 内 4K proxy 曾出现 p99 尖峰 25.7 ms 后自愈，属同类），但无丢帧遥测无法证实端到端后果。
- **建议动作（测量类先行）**：① 补端到端播放遥测（见 BENCH-0008）确认 30fps 时间线实际丢帧率；② 若确认，再评估优化方向（slice 粒度/预取深度/解码-呈现流水线），不在无遥测前动内核。
- **优先级**：P1。

## BENCH-0003 wrapper 绑定层 2.3–2.5× 解码开销

- **观察现象**：同一 120 帧真实素材解码，topos wrapper 路径 135.6 fps（p50 6.12 ms）vs native 直调 335.6 fps（p50 2.68 ms）——wrapper 慢 2.3–2.5×。
- **数据证据**：`bench_out/real_2k_h264_20260907/result.json`（decode_comparison 段；报告 playback.md §6.1）。
- **假设**：开销在 ctypes/Python 绑定层逐帧封包与平面拷贝（与 m11_stage3c 曾修的绑定层 .raw 精确拷贝 +48~60% 同族；cards-group3 m11_stage2b 卡）。
- **建议动作（测量类 → 优化类）**：① 先用绑定层剖析（E1 脚本对解码路径做同款 stage 拆分）定位拷贝/封包占比；② 确认后评估零拷贝平面交付（参照 m11_stage3a 零拷贝 +9% 与 3c +48~60% 的先例）；③ 注意三方对照像素格式/硬件路径不同（卡片已注明），复测须统一像素格式。
- **优先级**：P2（量级大，但产品路径是否走 wrapper 形态待确认——若产品已走 context 直解码则降级）。

## BENCH-0004 codec 98% 体积帽不满足 + QP63 地板三档超支

- **观察现象**：真实素材六档 TOPOS/ProRes 体积比全部 >1（1.03–3.79×），《TOPOS全格式ProRes对标与终轮优化计划》的"默认档容量 ≤ 对应 ProRes 98% 帽"验收线当前不满足；其中 proxy/pro444/extreme 三档 QP=63 地板锁死仍超支目标 bpp 1.40–1.67×，属帧内 VLC 内核压缩能力上限而非码控问题。
- **数据证据**：`bench_out/real_codec_bitrate_20260907/report.md` 结论段 + `result.json`（report codec.md §1.1/§3）；决策文档 `docs/codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md` §1/§6。
- **假设**：轻档与 12-bit 444 档的熵编码在低 bpp 目标下先于 QP63 失效；V2 熵编码曾带来 23–31% 码率收益（m9/c4082663），同方向还有空间但非调参可得。
- **建议动作（测量类 + 优化类）**：① 测量类：对三档地板格补 RD 扫描（≥5 质量点，决策 Q04 要求），确认"同质量更省"缺口；② 优化类：评估下一代熵内核（V3）或 proxy 档位定义本身（是否保留 3.79× 的档位语义）；③ 发布口径在此之前不得声称"更小"。
- **优先级**：P1（直接卡验收线）。

## BENCH-0005 不可复现基准脚本缺位

- **观察现象**：多数历史目录只有产物无脚本：4 臂解码对照（real_2k 的 decode_comparison）、readahead 证据链原文（已补官方脚本）、topos_quality 阶梯（脚本在但产物无命令行）、RD 扫描（6b3dfe9d 等，脚本疑似 tools/bench_rd_baseline.py 但产物未内嵌参数）、e1/d1 的原始运行参数需从 result.json config 逆推。已核实在库脚本：`tools/bench_topos_playback_path.py`（D1）、`tools/bench_encode_stages.py`（E1）、`tools/bench_topos_readahead.py`（成对差分）、`tools/bench_real_bitrates.py`（六档码率，report 附完整命令）、`tools/measure_topos_native_domains.py`、`tools/measure_topos_paired_quality.py`、`tools/probe_prores_vt_12bit_ramp.py`、`tests/benchmark/benchmark_timeline_occlusion.py`、`bench_out/codec_bench/run_bench*.py`（自带）。
- **数据证据**：`tools/` 目录清单（本次只读核查）；`bench_out/real_2k_h264_20260907/report.md`（无复现命令，对比 real_codec_bitrate 有）；REGRESSION_BASELINE.md 各锚点"复测"栏。
- **假设**：脚本多为临时 /tmp 或旧仓工作树产物，随窗口关闭丢失；锚点若无官方脚本则漂移带无法执行。
- **建议动作（流程类）**：① 为 ANC-06（4 臂解码对照）与 ANC-18（RD 扫描）补官方复现脚本；② 立规矩：所有入 `bench_out/` 的结果必须由 tools/ 或 tests/benchmark/ 内脚本产生，脚本路径写进 manifest；③ 现有 20 个锚点中已标"建议补基准脚本"的两处优先。
- **优先级**：P1（回归基线可执行性的前提）。

## BENCH-0006 翻盘数据未锚定参照（9a8bfd99 vs 058d29da）

- **观察现象**：同族 core/E2E 脚本两天两跑方向完全相反（058d29da 全 "ref faster" vs 9a8bfd99 全 "topos faster"，ratio 均 <1 但 2K core 0.777 vs 0.861、文件大小方向反转 1.84× vs <1），两目录 dylib 构建自不同工作树（topos vs topos_1），无 manifest 断言链证明参照（ffmpeg/PyAV）版本一致；且 interpretation 字段语义当期不稳定。
- **数据证据**：`bench_out/058d29da/37659ad5/a5b6fae6/20260902T234241Z/result.json`、`bench_out/9a8bfd99/58730875/f21fc30a/20260904T141812Z/result.json`（报告 ab-evolution.md §4）。
- **假设**：差异来自"构建 + 线程数 + 参照版本"三方混杂，正是 P0-04 manifest 规则的动因；也可能其中一次环境异常（ASan 窗口先例存在）。
- **建议动作（流程类）**：① 维持禁引该对的处置；② 修结果生成脚本：interpretation 必须由 ratio+CI 推导、参照版本（ffmpeg/PyAV）写入 manifest；③ 若想找回"翻盘"结论，按 P0-04 规则（同 clean HEAD + 同 dylib SHA + 交替 A/B + bootstrap CI）重跑一轮。
- **优先级**：P1（防止未来把噪声当 2× 优化/回归）。

## BENCH-0007 产品默认配置的端到端播放数字缺失

- **观察现象**：D1 是 readahead 关闭态的解码边界，readahead 是另一协议的稳态顺序吞吐，二者之间没有一次"当前默认配置（readahead on + arena + t16）、真实素材、含丢帧计数"的播放基准——ANC-01~05 与 ANC-07 不可数值复合。
- **数据证据**：`bench_out/d1_playback/*/…/result.json`（`read_ahead_depth=0` 诊断字段）；`bench_out/readahead/*.txt`（另一协议）；报告 playback.md §8.2。
- **假设**：默认配置下 2K/4K 各档的实际 fps 介于两套数据之间，但无人能给出数字；任何播放侧优化收益也无法在此缺口下验证。
- **建议动作（测量类）**：D1 脚本（`tools/bench_topos_playback_path.py`）加一档 `--read-ahead -1`（自动，即产品默认）的运行矩阵，与 readahead-off 成对输出；素材覆盖 4 档 + 4K H.264/HEVC（联动 BENCH-0009）。
- **优先级**：P2。

## BENCH-0008 端到端丢帧/卡顿遥测缺失

- **观察现象**：`docs/4k-playback-optimization.md` §4.2 要求的 `dropped_frames/late_frames/queue_depth/分段耗时` 播放日志字段在覆盖目录内无落盘；丢帧只能由 D1 p99 间接推断（BENCH-0002 的根因）。
- **数据证据**：报告 playback.md §2.3/§8.3（边界限制说明）；对照 `bench_out/extended_real_tests_20260917/edit_tracks_offscreen.log`（仅有 presented p50/p95 与 effective_fps，且 120/120 帧 CPU 回退属 T-P1-21 修复前状态）。
- **假设**：遥测字段未实现或未接入落盘；离屏 harness 的字段集（p50/p95/effective_fps）可作为起点。
- **建议动作（测量类）**：按优化文档 §4.2 字段清单补播放路径遥测并落盘 JSON；每帧记录 decode_ms/plane_prepare_ms/present_ms 与 dropped/late 布尔。
- **优先级**：P2（是 BENCH-0002 的前置）。

## BENCH-0009 4K H.264/HEVC 真实素材解码边界缺失

- **观察现象**：D1 只测 TOPOS 素材，real 系列只有 2K——用户最常见的"4K 互压缩素材卡顿"场景在边界数据中缺位；现有 4K 互压缩数字只有 p0_runner 单次（4K hevc 稳态 44.5 fps、随机 seek 376 ms）且 repeats=1 无 CI、commit 与 TOPOS 扫描轮不同。
- **数据证据**：`bench_out/d1_playback/` 素材清单（4 档全 TOPOS）；`bench_out/p0_runner/p0_runner_20260916_005041.json`（报告 playback.md §8.4、cards-group4）。
- **假设**：VT 硬解可用时 4K HEVC 边界不是瓶颈（p2_09 h265 单流 107 fps 剔预热口径佐证），但 VT 不可用回退软件路径的行为未测。
- **建议动作（测量类）**：D1 协议补 4K_H264/4K_H265（VT on/off 双态），与 TOPOS 4 档同表输出。
- **优先级**：P2。

## BENCH-0010 e1_encode native 相位字段全 0 未填充

- **观察现象**：E1 结果里 `native_stage_ms_per_frame`（fill/entropy/crc/dct 细相位）在全部 44 个 result.json 中均为 0（字段存在但未填充），native 内部相位分布只能回退到 09-02 的 m10 探针（口径已旧）。
- **数据证据**：`bench_out/e1_encode/*/…/result.json`（44/44 核对；报告 encode-export.md §1.1 注）。
- **假设**：`tc_encode_stage_stats` 读取路径在 E1 脚本中未接通（构建或 ABI 变更后失效）。
- **建议动作（流程类）**：修 `tools/bench_encode_stages.py` 的 stage 采集；修复前以 `TOPOS_CODEC_PROFILE=1` 探针为替代（4K XQ 试点已示范：entropy/fill 累计 189.8/110.7 ms）。
- **优先级**：P3。

## BENCH-0011 空间 AQ 收益证据冲突

- **观察现象**：两份独立数据方向相反——`64e7e219`（aq_retier，09-03）显示 AQ 省 1.5–4% 码率；`topos_quality/p3_spatial_aq_ab.json`（09-20）显示 AQ 三项全负（Y-PSNR −0.18~−0.27 dB、fps −7.3%、码率 +0.2%）。
- **数据证据**：`bench_out/64e7e219/48f15e80/480b40ff/20260903T181637Z/aq_retier.json`；`bench_out/topos_quality/p3_spatial_aq_ab.json`（报告 ab-evolution.md §2.1、quality.md §1.2）。
- **假设**：两者测的不是同一开关语义（定档重算 vs 编码时空间 AQ），或素材/档位差异主导；无统一口径前 AQ 默认值的正当性存疑。
- **建议动作（测量类）**：同素材同档同脚本一次 A/B 消融（AQ on/off × proxy/lt/hq），输出码率/PSNR/fps 三元组；结论前发布档位描述避免提及 AQ。
- **优先级**：P2。

## BENCH-0012 topos_quality 2K 4444xq MAE 指标异常未修

- **观察现象**：2K 4444xq 行 MAE≈14.3（4444 档为 4.1），与自身 PSNR 44.14 dB 严重不自洽，p0（14.83）与 p5（14.34）两轮同型存在——疑参考口径不含该档（12-bit 源 vs 10-bit 参考类问题）。
- **数据证据**：`bench_out/topos_quality/quality_recal-p5.json`（报告 quality.md §6.3）。
- **假设**：参考链（reformat/scale）在 12-bit 444 档下位深/色度抽样错配，只影响 MAE 不影响 PSNR/SSIM 的计算路径。
- **建议动作（流程类）**：修 `tools/measure_topos_baseline_quality.py` 的参考口径或在输出中为该档自动标记 invalid；修复前该行 MAE 禁引（已写入 REGRESSION_BASELINE 不可比清单 #9）。
- **优先级**：P3（不影响当前结论，但污染未来自动化对比）。

## BENCH-0013 Q01 配对证据需扩样 + RD 曲线（Q04 决策依赖）

- **观察现象**：配平码率下 Topos 全档 Y-PSNR 领先 prores_ks +7.5~+27.2 dB（30 帧），但样本仅 30 帧/单素材/有损解码源、参照是 ffmpeg prores_ks 而非 Apple VT；决策文档 Q04 要求的"≥5 质量点 RD 曲线"证据不存在——"更多比特换更高 PSNR"与"同质量更省"的区分仍无定论。
- **数据证据**：`bench_out/topos_paired_q01_real_source/*-match/paired_*.json`（报告 quality.md §3.1、codec.md §1.3）；`docs/codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md` Q04。
- **假设**：当前结论对 prores_ks 成立、对 Apple 编码器未证；且配平点单一，曲线斜率未知。
- **建议动作（测量类）**：① 扩样（≥3 素材 × ≥5 码率点，复用 `tools/measure_topos_paired_quality.py`，命令行已在 JSON 内）；② 补一组 Apple VT 侧参照（脚本已具备 p416le 对接经验）；③ 输出 RD 曲线供 Q04 判定。
- **优先级**：P2（与 BENCH-0004 同链条，那是体积侧、这是质量侧）。

## BENCH-0014 4K XQ 编码线程轴枯竭，收益应转向流水线/熵段

- **观察现象**：4K XQ E1 分段 12→16 线程仅 +1.5%、16T p95 41.63 ms 仍高于 4K25 的 40 ms 预算；历史证据显示该机 t8 后普遍饱和（m1063/m103/0b6322d8 扫描），另一台 16C24T 机则 8→16 仍有 +52%/+70%——线程结论强依赖物理核数。
- **数据证据**：`bench_out/topos_perf_thread_refine_3rounds.json`；`docs/codec/evidence/TOPOS_2026-09-27_local_pilot/performance_4k_xq_thread_sweep_5rounds.json`；`bench_out/m104_pipeline/20260902_010250_2k/result.json`（流水线 2K +54%/4K +22% 先例）；报告 native-threading.md §1.3/§4。
- **假设**（原文标注为推断）：E1 分段含 Python prep/mux 串行成分，掩住纯内核 t16 惩罚；下一档收益更可能来自 m104 式流水线化或熵段内核（P01 探针 entropy/fill 累计占比提示），而非加线程。
- **建议动作（优化类，前置测量）**：① 先在真实整链竞争（导出时解码/渲染并行）下复测线程曲线（P02 已列）；② 试点解码-编码流水线重叠或熵段批量化，门禁沿用 3% 回退线 + 位流不变断言。
- **优先级**：P2。

## BENCH-0015 timeline_occlusion 复测无 CI + 2K 版无有效组

- **观察现象**：4K B1 的 3.67× 是 runs=2 无 bootstrap CI 的观察值；2K 版只有 B0 单轨组（culled_ratio=0），剔除机制未激活，1.027×"加速"为噪声——2K 场景的剔除收益至今无证据。
- **数据证据**：`benchmarks/timeline_occlusion_bench_result_4k.json`、`benchmarks/timeline_occlusion_bench_result.json`（报告 native-threading.md §3、misc.md §5）。
- **假设**：机制本身有效（B1 结构量 140→70 精确成立），只是测量协议不足。
- **建议动作（测量类）**：复测补 runs≥10 + bootstrap CI；2K 补 B1/B2 组；结果纳入 manifest 树。
- **优先级**：P3。

## BENCH-0016 native 域基准 toos CLI 三代 SHA 混用

- **观察现象**：native 域三目录（matrix / float_zero_audit / raw_cfa）的 toos CLI SHA 分别为 06508c5c / 4b8e5366 / 333b7df3（audit 与 matrix 间 CLI 已升级，靠"编码字节逐档一致"交叉验证码流未变）；跨 CLI 版本的数字差异会被误读为回归。
- **数据证据**：`bench_out/topos_native_domain_matrix/*/native_domains_*.json`、`bench_out/topos_native_domain_float_zero_audit/native_domains_float16.json`（报告 native-threading.md §2/§6 勘误 2）。
- **假设**：CLI 迭代正常，但基准协议未固定"复测必须同 CLI SHA 或先跑字节一致性对照"。
- **建议动作（流程类）**：复测协议加两条：CLI SHA 不一致时先比对编码字节；把 CLI/脚本 SHA 校验做成 `tools/measure_topos_native_domains.py` 的前置断言。
- **优先级**：P3。

## BENCH-0017 锚点级数据未纳入 P0-04 manifest 结果树

- **观察现象**：被选为回归锚点的多个来源不在 manifest 化体系内：readahead（txt 无 manifest）、real_2k（旧工作树构建无 SHA）、export_baseline（无 git/dylib manifest、单次）、p0_runner（repeats=1、run 间 commit 不同）、timeline_occlusion（无 manifest）；dirty 构建的 e1 试点（ANC-16 绑定 1d30609f dirty）也是锚点。
- **数据证据**：`bench_out/HISTORICAL.md`（仅 058d29da 为当前有效树）；各目录卡片可比性备注（cards-group2/4/5）。
- **假设**：锚点若长期挂在 dirty/无锚定数据上，漂移带形同虚设（无法区分代码回归与构建差异）。
- **建议动作（流程类）**：为 ANC-06/07/13/16/20 各做一次 clean HEAD + manifest 化的"锚点转正"运行，结果按 `<commit>/<dylib8>/<manifest8>/<utc>/` 追加进结果树，此后漂移带以转正值为基。
- **优先级**：P2（是全部性能锚点生效的前置）。

---

## 对账表

| ID | type | severity | 一句话 |
|---|---|---|---|
| BENCH-0001 | risk | high | 首帧/冷启动零数据 |
| BENCH-0002 | risk | high | 4K 4444xq p99 击穿 30fps 预算 |
| BENCH-0003 | perf-opportunity | medium | wrapper 层 2.3–2.5× 解码开销 |
| BENCH-0004 | risk | high | 98% 体积帽不满足 + QP63 地板 |
| BENCH-0005 | process | high | 不可复现基准脚本缺位 |
| BENCH-0006 | process | high | 翻盘数据未锚定参照 |
| BENCH-0007 | risk | medium | 默认配置端到端播放数字缺失 |
| BENCH-0008 | risk | medium | 丢帧/卡顿遥测缺失 |
| BENCH-0009 | risk | medium | 4K H.264/HEVC 解码边界缺失 |
| BENCH-0010 | process | low | native 相位字段全 0 |
| BENCH-0011 | risk | medium | AQ 收益证据冲突 |
| BENCH-0012 | process | medium | 4444xq MAE 指标异常 |
| BENCH-0013 | risk | medium | Q01 扩样 + RD 曲线 |
| BENCH-0014 | perf-opportunity | medium | 线程轴枯竭转流水线/熵段 |
| BENCH-0015 | process | low | occlusion 无 CI + 2K 无有效组 |
| BENCH-0016 | process | low | toos CLI 三代 SHA 混用 |
| BENCH-0017 | process | medium | 锚点未入 manifest 树 |

共 17 条，与 `FINDINGS.json` 条目数一致。
