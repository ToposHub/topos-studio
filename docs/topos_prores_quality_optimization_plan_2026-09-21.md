# TOPOS 与 ProRes 画质对齐优化计划（2026-09-21）

## 1. 结论与范围

本轮真实素材复验确认：TOPOS Proxy 的问题**不只是码率偏低**。

- 当前六档确实命中 `target_bpp` 的既定码率目标，码控没有失效；但该目标在当前
  素材上明显低于同档 ProRes 的实际码率，Proxy 最严重。
- 同一段 2K ProRes 422 Proxy 输入、近似相同文件体积下，H.264 的细节保留明显优于
  当前 TOPOS Proxy。因此不能只把 TOPOS 档位的码率抬高后就宣称解决；低码率帧内
  编码的量化、空间比特分配和块伪影控制必须一并优化。
- TOPOS 当前是逐帧帧内编码；H.264 测试使用帧间预测，后者天然可以利用静态画面的
  时间冗余。它不是严格的 codec 结构对等比较，但它是用户可见质量的有效基线；
  ProRes 同档、同尺寸、同帧数素材仍是“中间编码格式”主验收基线。

本计划的产出目标是：每档 TOPOS 在**不超过同档 ProRes 完整文件大小**的硬上限内，
改善纹理、边缘和渐变；不以时间线显示、色彩元数据或音频问题混入本次画质结论。

## 2. 已取得的实测证据

### 2.1 2K Proxy 同输入、同帧数、同量级码率实验

输入为 `tests/test_videos/2K_prores422Proxy.mov` 的前 60 帧（1920×1080、25fps、
YUV 4:2:2 10-bit）。指标在第 0/15/30/45/59 帧对输入解码 RGB8 计算；它用于比较
同一输入的相对变化，不替代高位深 YUV/主观评测。

| 编码 | 视频 bpp | 60 帧文件大小 | RGB8 PSNR | RGB8 MAE | 观察 |
| --- | ---: | ---: | ---: | ---: | --- |
| 当前 TOPOS Proxy | 0.2896 | 4,503,702 B | 34.556 dB | 3.037 | 藤编与花纹靠垫出现清晰 8×8 方块 |
| H.264 High 4:2:2 10-bit（码率匹配） | 约 0.310 | 4,816,311 B | 41.329 dB | 1.189 | 与源接近，纹理连续性明显更好 |
| TOPOS Proxy（ProRes Proxy 码率上限的 98%） | 0.7064 | 10,985,247 B | 43.313 dB | 1.143 | 方块显著减少，但体积约为当前 Proxy 的 2.44 倍 |

直接结论：

1. 同量级文件（4.50 MB vs 4.82 MB）下，H.264 比当前 TOPOS Proxy 高 **6.773 dB**，
   MAE 低 **60.8%**。当前 Proxy 的量化/空间编码效率存在实质性优化空间。
2. 把 TOPOS Proxy 提到 ProRes Proxy 码率天花板附近，PSNR 增加 **8.757 dB**、MAE
   降低 **62.4%**。提高码率是立刻有效的缓解措施，但不能替代编码器优化。
3. 放大检查的高纹理区域（藤编和花纹靠垫）表明伪影与 8×8 DCT 块边界一致，属于
   高 QP 后高频系数大量归零的典型块效应，而非显示颜色转换问题。

### 2.2 当前测试文件与同档 ProRes 的视频码率比

以下为已验证帧数、时长一致的对照项；`rate` 只比较视频流，音频均为 PCM，不计入
该列。TOPOS 当前没有超过 ProRes，但很多档位留下的体积余量过大。

| 尺寸 | 档位 | TOPOS Mbps | ProRes Mbps | TOPOS / ProRes |
| --- | --- | ---: | ---: | ---: |
| 2K | 422 Proxy | 15.7 | 37.0 | 42.4% |
| 2K | 422 LT | 49.7 | 80.3 | 61.8% |
| 2K | 422 | 78.5 | 119.9 | 65.5% |
| 2K | 422 HQ | 132.5 | 181.7 | 73.0% |
| 2K | 4444 | 222.0 | 273.3 | 81.2% |
| 2K | 4444 XQ | 326.8 | 402.7 | 81.1% |
| 4K | 422 Proxy | 62.4 | 143.6 | 43.5% |
| 4K | 422 LT | 199.5 | 301.2 | 66.2% |
| 4K | 4444 | 888.0 | 1060.5 | 83.7% |
| 4K | 4444 XQ | 1311.2 | 1613.4 | 81.3% |

### 2.3 现有对照资产的阻塞项

- `4K_prores422HQ.mov` 是 743 帧 / 29.72 秒，而当前 `4K_topos_422_HQ.mov` 是
  487 帧 / 19.48 秒，不能作为同档体积或同帧画质对照。
- 测试目录没有与当前 4K TOPOS 422 Standard 对应的 `4K_prores422.mov`。
- 因此，4K 422 Standard/HQ 在完成“同母版、同帧数、同音频、同色彩标签”的 ProRes
  基准集之前，不能设定最终码率上限，也不能写入产品宣称。

## 3. 根因定位

### 3.1 码率预算偏低（确定）

`src/shared/codec/topos_profiles.py` 中各档 `target_bpp` 是静态值；当前 Proxy 为
`0.29108 bpp`。本次 2K ProRes Proxy 的实测是 `0.7138 bpp`，也就是当前目标约为
该实际 ProRes 码率的 41%。4K Proxy 同样只有对应 ProRes 的约 43.5%。

`ToposVideoEncoder._encode_packet()` 把该 bpp 换算为每帧 `target_bytes`，首帧用
`encode_sized()` 搜索 QP，后续由 `ToposRateFeedback` 以整帧字节反馈维持长期均值。
它确实把文件压到目标附近，但该目标不适合这段高纹理素材。

### 3.2 低码率帧内压缩效率与块伪影（确定）

- `native/topos_codec/src/codec/intra.c` 对颜色块使用固定 **8×8** 前向/逆向变换。
- `native/topos_codec/src/transform/quant.c` 的 `qp_scale` 在 Proxy 实际工作区间
  （历史真实 2K 基准 QP 均值约 86）非常粗；纹理 AC 系数容易整体归零。
- 422 视频档当前使用 `qmatrix_id=0`（flat），它保留频率权重的中性语义，却没有
  内容自适应比特分配或块边界去伪影机制。低码率下会直接呈现 DCT 块。
- 当前 `aq_mode` 的收益受限于色度/切片粒度，不能解决纹理块的亮度细节分配；
  `rdo_mode` 在现有产品默认熵模式下已有负收益记录，不能作为即时开关。

### 3.3 当前接口无法表达“不得超过某个 ProRes 文件”的产品约束（确定）

`FFmpegEncoderConfig.bitrate` 是通用字段，但 TOPOS 编码路径目前没有将它作为
`target_bytes` 覆盖；它始终优先使用档位的静态 `target_bpp`。因此不能对一次导出
传入“本次输出不超过某个基准文件 / 某个字节上限”的精确预算，只能修改全局档位表。

## 4. 目标与验收门槛

### 4.1 容量约束

对每个可比基准文件建立硬上限：

`TOPOS 总输出字节 <= 对应 ProRes 总文件字节 × 0.98`

编码预算必须先扣除已知音频、容器预留和索引开销，再分配给视频；禁止只比较视频流
后让最终 MOV 因音频或封装开销超过 ProRes。

### 4.2 画质验收

每档在至少三类真实片段上运行：高纹理（藤编/织物）、平滑渐变（天空/墙面）、
运动/场景切换。每类不少于 120 帧，2K 和 4K 均覆盖。

| 检查项 | 门槛 |
| --- | --- |
| 容量 | 最终文件不超过匹配 ProRes 的 98% |
| 帧/音频/色彩 | 帧数、时长、PCM 音频、色彩元数据与基准一致 |
| 客观指标 | 记录 Y/U/V PSNR、SSIM、MAE；与当前基线比较不得退化 |
| 块伪影 | 新增 8×8 边界残差指标；Proxy 先以当前版本为基线，迭代时持续降低 |
| 主观评测 | 200% 和 400% 下双盲看藤编、织物、文字、肤色、渐变；不得出现明显棋盘/方块 |
| H.264 参照 | 同码率 H.264/H.265 仅作用户体验参照，不因其帧间结构而替代 ProRes 主验收 |

## 5. 分阶段优化计划

| 阶段 | 目标 | 具体工作 | 主要文件 | 完成标准 |
| --- | --- | --- | --- | --- |
| P0：基准清洗 | 先让比较可信 | 从同一个 4K ProRes 4444 母版生成 2K/4K 六档 ProRes 与 TOPOS；固定 487 帧、25fps、音频、BT.709 limited；生成 manifest | `tests/test_videos/`、测试脚本 | 每一对分辨率、帧数、时长、色彩、音频一致 |
| P1：预算接口 | 不超过 ProRes 且用足预算 | 为 TOPOS 增加 `max_video_bytes` / `max_file_bytes` 优先级；由导出器预留音频和 MOV 开销；`bitrate` 可显式覆盖 tier bpp | `src/shared/export/topos_encoder.py`、`src/shared/export/video_encoder.py` | 每个输出被硬限制在 98% 上限内，且不靠手工改全局 tier |
| P2：档位重标定 | 先消除当前最明显块感 | 以 P0 基准重新测每档 bpp；为 Proxy/LT/Standard/HQ 设 ProRes 容量上限下的默认目标；保留旧低码率为单独“网络代理”档，不再命名为 ProRes 对标档 | `src/shared/codec/topos_profiles.py` | 新 12 文件全部低于上限；Proxy 放大观看不再有当前级别网格块 |
| P3：空间 AQ | 同码率优先保纹理与边缘 | 采集每 8×8 块的梯度/方差/边缘方向；在固定帧预算内从平坦区回收码字，分配给藤编、织物、文字；先实现 slice/区域级验证，再决定是否需要块级 QP 信令 | `native/topos_codec/src/codec/intra.c`、`native/topos_codec/src/transform/quant.c`、`src/shared/export/topos_rate_control.py` | Proxy/LT 同码率客观指标提高，8×8 边界残差下降，编码/解码速度不突破预算 |
| P4：帧级码控 | 防止复杂帧被平均码率饿死 | 用帧复杂度前瞻替代单纯上一帧反馈；加入场景切换检测、短窗 VBV、复杂帧借贷与封顶；输出 QP/实际字节遥测 | `src/shared/export/topos_rate_control.py`、`src/shared/export/topos_encoder.py` | 复杂帧不再出现大幅质量下坠；全片仍满足字节硬上限 |
| P5：核心帧内工具 | 从根上降低方块效应 | 评估 4×4/8×8/16×16 自适应变换、变换跳过、方向性帧内预测、面向纹理的量化矩阵；新工具必须使用显式 bitstream version/profile，旧解码器不可静默误读 | `native/topos_codec/src/codec/intra.c`、`native/topos_codec/src/transform/*`、`native/topos_codec/src/bitstream/*` | 在 P2 容量上限下接近/超过预设客观与主观门槛；完整互操作回归通过 |
| P6：去块策略 | 避免把伪影留给预览层 | 只有 P3/P5 后仍存在可见块时，再实现规范化的 encoder/decoder 一致去块滤波；不得只在 GUI 预览偷偷磨皮 | codec core + bitstream version | 伪影降低且不牺牲边缘；CPU/GPU/缩略图结果一致 |
| P7：回归与发布 | 防止再次误标定 | 将真实素材量化报告、源文件 hash、容量、PSNR/SSIM、缩放细节图和阈值接入 CI；更新白皮书与 UI 档位说明 | `native/topos_codec/tools/`、`native/topos_codec/tests/benchmark/`、`docs/` | 任一档位超过 ProRes 上限、主观块伪影回归或元数据漂移均阻断发布 |

## 6. 建议的首个可交付版本（P0–P2）

先不要直接修改变换内核。首个版本应只完成基准、预算与重标定，风险最低且可以立即
改善当前测试文件：

1. 生成缺失的 4K ProRes 422 / 422 HQ 同母版基准；删除“帧数不同也参与对比”的结论。
2. 在编码配置中实现按**最终文件总大小**约束的 TOPOS 视频预算。
3. 对每个档位以 98% ProRes 上限做一次全片编码，选择不超过上限的最佳工作点。
4. 输出新的 12 个文件、manifest、同帧 200%/400% 对照图和指标报告。
5. 若 Proxy 在这个工作点仍明显劣于 H.264 体验基线，再进入 P3；不要只继续扩大
   Proxy 文件体积，否则会失去 TOPOS 的体积/速度定位。

## 7. 当前 ProRes 上限建议（P0 前仅供试验，不写入产品默认）

下表是已同帧/同时长项目的“视频流 98%”试验值，最终仍以**总文件字节硬上限**为准。

| 尺寸 | 档位 | 可用 ProRes 视频 Mbps | TOPOS 试验上限 Mbps（98%） |
| --- | --- | ---: | ---: |
| 2K | 422 Proxy | 37.0 | 36.3 |
| 2K | 422 LT | 80.3 | 78.7 |
| 2K | 422 | 119.9 | 117.5 |
| 2K | 422 HQ | 181.7 | 178.1 |
| 2K | 4444 | 273.3 | 267.8 |
| 2K | 4444 XQ | 402.7 | 394.6 |
| 4K | 422 Proxy | 143.6 | 140.7 |
| 4K | 422 LT | 301.2 | 295.2 |
| 4K | 4444 | 1060.5 | 1039.3 |
| 4K | 4444 XQ | 1613.4 | 1581.1 |

4K 422 Standard 与 HQ 暂缺合格基准，必须在 P0 补齐后填入，不能从其它档位插值猜测。

## 8. 不应采用的“快速修复”

- 不要仅修改缩略图、时间线或 GPU 着色器来模糊方块：这会掩盖码流问题，并让导出、
  其他播放器和 CPU 解码路径继续劣化。
- 不要把 H.264 的帧间测试直接当作 TOPOS 帧内效率的等价证明；应保留它为体验参照，
  同时以 ProRes 帧内基准做产品验收。
- 不要在没有总文件预算的情况下盲目提高全局 `target_bpp`；不同分辨率、内容和音频
  会导致最终 MOV 超过用户要求的 ProRes 上限。
- 不要默认打开现有 RDO/AQ 开关作为“画质修复”；先按 P3 测量真实素材收益，避免已知
  的低码率负收益和编码速度回退。

## 9. 实施状态

- **P0（基准清洗）已完成（2026-09-21）**：以 `4K_prores4444.mov`（Apple ProRes 4444
  母版，3840×2160、487 帧、25fps、BT.709 limited、pcm_s24le 48k stereo）为唯一母版，
  补齐 `4K_prores422.mov` 并重建异源 743 帧的 `4K_prores422HQ.mov`（Apple
  VideoToolbox ProRes 编码器，487 帧、音频 PCM 直拷）；12 对 ProRes/TOPOS 基准全部
  通过分辨率/帧数/时长/色彩/音频一致性校验（TOPOS 音频与源 PCM **字节级一致**）。
  生成工具 `tools/generate_topos_baseline_corpus.py`，清单
  `tests/test_videos/topos_prores_baseline_manifest.json`。同母版证据链：基准集与母版
  音频净载荷 sha256 一致；帧 0 内容 PSNR（2K 4444 45.9 dB / 4K 4444XQ 59.0 dB vs
  母版下采样）确认同源。P0 时刻现状：TOPOS/ProRes 体积比 46.7%–83.8%，Proxy 档
  留白最大（2K 48.1% / 4K 46.7%），4K 422 Standard 实测可用 ProRes 视频码率
  470.35 Mbps（此前缺失的 §7 表项）。
- **P1（预算接口）已完成（2026-09-21）**：`FFmpegEncoderConfig` 新增
  `max_video_bytes` / `max_file_bytes` / `budget_frames`，Topos 编码路径三级预算
  优先级 `max_file_bytes > max_video_bytes > bitrate > 档位 bpp`；文件预算自动
  扣除音轨（PCM 精确 / AAC 按声明码率估算）与容器预留（4KB + 64B/帧，P0 语料
  实测 29.6B/帧）；`ToposRateFeedback` 预算模式逐帧目标 = 剩余预算均摊 ×0.97
  安全系数；close() 超限拒绝定稿。真实验证：2K Proxy @ ProRes 98% 上限 →
  97.98% of ProRes（36.2 Mbps，2.18× 旧档）。
- **P2（档位重标定）已完成（2026-09-21）**：422 家族四档 `target_bpp` 由
  ADR-C043 等画质定标改为 ProRes 容量上限下的默认目标（同母版实测 ProRes bpp
  取紧端 ×0.98 ×0.95）：proxy 0.29108→0.64483（2.22×）、lt 0.94075→1.35234
  （1.44×）、standard 1.50296→2.11179（1.41×）、hq 2.54956→3.15075（1.24×）。
  旧 Proxy 低码率档保留为新 **netproxy** 档（容器 tpcD tier id 复用 1，label
  区分——native tier 域上限 7，新增 id 属容器规范变更且旧读端判 MALFORMED）。
  444 家族维持原定标（实测已占 ProRes 81–84%）。新 12 文件全部低于 98% 上限
  （TOPOS/ProRes 落点 88.0%–93.6%）。重标定后质量（26 抽样帧，母版参考，
  vs P0 基线）：2K proxy PSNR Y +4.34 dB、块指数 3.949→1.918；4K proxy
  +2.72 dB、2.938→2.060；lt/standard/hq 全档 PSNR/SSIM/MAE 提升，无任何
  指标回退；444 家族字节不变。度量工具
  `tools/measure_topos_baseline_quality.py`（Y/U/V PSNR、SSIM、MAE、8×8 块
  边界残差指数），结果存 `bench_out/topos_quality/quality_{baseline-p0,
  recal-p2}.json`。
- **P3（空间 AQ，slice 级验证）已完成并给出否决证据（2026-09-21）**：
  亮度空间 AQ 已实现并入 `aq_mode='on'` opt-in 面（plane 0 块均值活动度、
  1/8 倍频程定点 log2——修复 ilog2 的 2× 频程粒度把 1.5× 活动度比折进同格、
  偏移不触发的旧根因；纹理优先方向、强度 3×钳位 ±4；变体阶梯 5→9 档；
  位流零格式变更，qp_delta_biased 逐 slice 承载，78 项原生测试与 golden
  全过）。合成验证：上忙/下平帧忙带重建 MAE 21→1（test_stage9）。但同母版
  真实语料 A/B（2K/4K × proxy/lt，487 帧，tier bpp 同码率，
  `bench_out/topos_quality/p3_spatial_aq_ab.json`）：ΔPSNR_Y −0.05~−0.27 dB、
  ΔSSIM ≤0、8×8 块指数持平（±0.01）、编码吞吐 −8%~−23%——**带粒度亮度
  重分配在掩蔽与纹理优先两个符号方向均为负收益**（与 2026-09-03 全平面
  实验互证）。按 P3 验收门槛（客观指标不得退化）判定：slice/区域级空间
  AQ 不启用，`aq_mode` 默认关维持；是否需要块级 QP 信令（位流版本变更）
  归入 P5 评估。P2 重标定后 Proxy 块指数已 1.92/2.06（基线 3.95/2.94），
  无 P3 残留的可见块问题压力。
- **P4（帧级码控）已完成（2026-09-21）**：`ToposRateFeedback` 重构为比例 + 短窗
  VBV（24 帧窗）+ 长程积分 + 借贷偿还（封顶 3×target、12 帧窗温和偿还、每帧
  ≤2 qp）的复合控制律；切换检测（帧字节 >1.8× 窗口中位数）置 `reseed_hint`。
  编码器侧：帧内复杂度前瞻（Y 平面 16×4 采样梯度，编码前计算）或 rc 切换
  信号触发 sized 搜索**即刻重锚**（复杂度突变首帧直接拿到正确 qp，冷却 8 帧
  防闪烁反复），`anchored()` 只换 qp 不清预算累计；close 输出 QP/字节/切换/
  借贷遥测，`rate_control_telemetry()` 可编程获取。验证：合成阶跃序列
  （平→噪→平）噪声首帧 qp 直接 90（旧律需 ±4/帧爬行 ~20 帧）、回平首帧即时
  归位；真实 2K proxy 全片 487 帧贴目标 99.1%、字节级确定性、qp∈[74,87]
  平滑、借贷 23KB 全偿；重生成语料 12 对全部 ≤98% 上限（80.6%–92.8%），
  质量与 P2 持平（±0.18 dB，`quality_recal-p4.json`）。新增
  `tests/media/test_topos_frame_rc.py`（8 项）。
- **P5（核心帧内工具，评估轮）已完成评估（2026-09-21）**：
  1. **档位对标基线补齐**（同母版参考、26 抽样帧、YUV 域）：2K 上 TOPOS
     各档与 ProRes 打平或反超（换缩放器参考噪声下 proxy 35.76 vs 35.75、
     hq 42.89 vs 42.34 dB）；4K（纯编码对比，无缩放）TOPOS 落后
     proxy −0.65 / lt −1.9 / standard −6.7 / hq −5.8 dB（@ 体积 92–93%）。
  2. **代际隔离实验**：TOPOS 直接从母版编码（4K standard 432 Mbps →
     51.80 dB / hq 647 Mbps → 58.41 dB）vs 同档 ProRes（56.96 / 62.08 dB）
     ——差距**不是**语料转码链的代际叠加，是帧内编码效率本身的差距。
  3. **根因定位**：逐帧 PSNR 分布两极（简单段 57–61 dB / 高纹理段
     48–52 dB，qp 66→75）；单帧 RD 探针（qp50 → 1745 Mbps@122 dB /
     qp60 → 823 Mbps@66.6 dB）证实**高 qp 区量化步长过粗**（kQpScale
     每 8 qp 步长翻倍，qp≥70 处步长相对 10-bit 码值过粗，纹理 AC 整体
     归零）正是计划 §3.2 所指根因。
  4. **工具裁决**（P5 清单逐项）：
     - **go（下一轮最高 ROI）**：高 qp 区量化步长细化（kQpScale 表
       qp>60 段放缓倍率）——直接攻击 48–50 dB 硬帧平台；须显式
       bitstream minor version（旧解码器拒读新流，符合"不可静默误读"）。
       **首轮 A/B 已测（2026-09-21 晚，锯齿诊断文档 §5.1）**：/6 细化表
       仅 4K standard 胜（+0.53 dB 全图 @ −0.1% 体积），2K proxy/hq 与
       4K proxy 均小负（−0.10~−0.24）——收益随内容谱型（原生 4K 纹理）
       而非 qp 段分化；采纳形态收敛为**按档位选表 + em 版本载体**，并
       实证了旧解码器静默误读异表码流（12.7 dB 垃圾画面），版本门控
       为物理必需。2K 主诉需同载体捆绑 P6 去块滤波。
     - **evaluate**：4×4/16×16 自适应变换、变换跳过、方向性帧内预测
       ——潜力最大但均为块级信令位流工程，须在新版本载体上立项。
     - **no-go（证据充分）**：面向纹理的量化矩阵——ADR-C033（flat 对
       Standard 全胜）+ P3（频域/空域重分配两方向同码率负收益）表明
       均匀量化在本码流上就是 PSNR 最优；块级 QP 信令（空间 AQ 载体）
       同理由 P3 带级负收益降级为低优先。
  本轮不落地位流版本变更（新量化表/新变换须 golden 全量重生成 +
     互操作矩阵 + 解码器分发周期，属独立战役，P7 回归基建先行）。
  5. **主观对比素材**（§4.2）：`bench_out/topos_quality/p2_subjective_*
     {netproxy_old,proxy_p2,prores_ref}_{200x,400x}.png`（高纹理帧
     中心裁片）——视觉模型评估：旧校准 netproxy"严重马赛克化、块边界
     断裂"；P2 重标定后 proxy"块效应轻微至中等、纹理保持连续"（vs
     ProRes 参考"接近视觉无损"）。块效应主诉按 P2 工作点解除。
- **P6（去块策略）已落地 v1（2026-09-21 深夜，锯齿战役）**：用户报告
  "P2 后仍存在可见块"满足启动门槛。输出端 8×8 网格去块（编解码一致，
  qp 自适应强度 + 真边缘保护）经 flags bit2 版本载体制品流化；与 QPT2
  细化表（4K standard/hq）同批发布。v1 强度保守（客观指标 +0.02~+0.53
  dB、块指数微降），主观效果以用户对比为准；强度调优与缩放交付滤波
  归后续批次。原"暂缓"裁决撤销。
- **P6（去块策略）原裁决：暂缓（2026-09-21）**：门槛"只有 P3/P5 后仍存在
  可见块时"——P2 重标定后 proxy 块指数 1.92/2.07（基线 3.95/2.94），
  400% 视觉评估为"轻微至中等、纹理连续"；规范化 encoder/decoder 一致
  去块滤波属解码端位流语义变更（同 P5 版本战役载体），与高 qp 细化
  合并立项评估，不单独实施；禁止只在 GUI 预览磨皮（§8 红线）。
- **P7（回归与发布）已完成（2026-09-21）**：
  1. **发布门禁** `tools/verify_topos_quality_gates.py`：容量（任一档位
     topos > prores×98% 阻断）、质量（12 格 PSNR/SSIM 地板 + 8×8 块指数
     天花板，锚定 P2/P4 交付值留回归容差）、漂移（档位表 target_bpp 偏离
     v1.9 定标、manifest 成对一致性、素材/母版 sha256 指纹）三类回归全部
     阻断；`--require-media` 素材在场复核 hash，`--json` 机器读；负向测试
     验证拦截有效。接入 `.github/workflows/codec-matrix.yml` python-app
     job（素材不在场时校验已提交 manifest 与质量报告），新测试文件
     （test_topos_budget/test_topos_frame_rc）同步入 pytest 门禁。
  2. **文档**：白皮书（中英）§4 档位表更新到 v1.9（netproxy 新行、
     ProRes 容量重标定说明、QP 域 0–95、预算接口提及）。
  3. **全量回归**：native ctest 78/78、Python topos 套件 419/419（音频
     E2E 需 ffmpeg CLI 在 PATH——CI 已装；本机以 static_ffmpeg bin 挂
     PATH 复验通过）、质量门禁 PASS（含 24 文件素材指纹复核）。
