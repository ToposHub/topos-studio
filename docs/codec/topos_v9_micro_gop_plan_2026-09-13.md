# V9 帧间微 GOP 落地计划（Topos 422 LP：topos_v9_micro_gop_plan）

日期：2026-09-13 · 状态：**批 0 已完成（2026-09-14，M1 拍板见 ADR-C047：只做 zero-motion IP-2，批 4 P1 取消、批 7 IP-4 降级可选）；批 1 已完成（2026-09-14，格式冻结 V8 同构 + GOP context 骨架）；批 2 已完成（2026-09-14，zero-motion IP-2 端到端 + golden_v9 入库）；批 3 已完成（2026-09-14，MOV v9 容器 + prev_sync + CLI GOP 感知 + golden_mov_v9 入库）；批 5 已完成（2026-09-14，绑定 GOP context + 编码器 gop='ip2' + 源 V9 读路径 + manifest v9 段）；**批 6 评审完成（§7 门不达，LP 不注册，V9 保持 experimental）；批 7 不执行——窗口结项（2026-09-14）；结项复审完成（2026-09-14，9 处问题修复：BUFFER_TOO_SMALL 参考污染 / conceal 帧入参考 / V9 写口径漏闸等，见 §12）**；排期前序 TRAW 批 1~5 已于 2026-09-13 结项
产品目标：**Topos 422 LP（长播放档）**——LT 同画质、名义 ≈150 Mbps@4K30 的帧间微 GOP 档
取代：`Topos_V2.1_Micro-GOP 可落地升级计划（2026-08-30，桌面稿，未入库）`——其全部工程纪律
（重建参考、I 回退、spike 先行、数字不预承诺）继承进本文批 0~7；其针对 v1.1 Rice 基线的
格式假设全部作废，按当前 V8/V9 窗口结构重写。
关联：`topos_v8_format_plan_2026-09-12.md`（V8 段化熵窗口，批 0~5 已结项——V9 的熵层底座）、
**ADR-C046（位流代际生命周期与退役规程，2026-09-13）——V9 新 major 立项须按其 §4 版本预算纪律登记入产判据/golden 隔离/前序死轴退役评审点，em/major 编号遵守封存规则**、
**ADR-C047（LP Spike M1 拍板，2026-09-14）——只做 zero-motion IP-2：批 4 P1 取消、§3.4 预测层级冻结 P0、批 6 按 §7 实测评审、批 7 IP-4 降级可选**、
`topos_traw_format_plan_2026-09-12.md`（排期前序；其 §7 明确 RAW 窗口不做帧间，互不冲突）、
ADR-C027（版本规则单点）、ADR-C036（V7-R2）、ADR-C045（编码后向环判死）。
适用读者：**全新会话的执行 agent**——本文档自包含，按 §0 启动。

---

## 0. 新会话引导（先读这节）

**项目**：Topos Color 图像/视频编辑器，自研 topos_codec（C 库 + Python 绑定）。
本计划 = 在现有 codec 上增加 **V9 位流窗口（帧间微 GOP）**：I/P 帧、闭合 GOP≤4、
仅前向参考、无 B 帧，产品形态为 **Topos 422 LP** 档（剪辑域长播放/消费体积档）。
**不动 V1..V8 任何既有位流语义**（golden SHA 零变化是硬门禁）。

启动 checklist：

```bash
cd "/Users/testuser/Documents/vscode/topos_1"
# 构建/测试（三个命令就是全部门禁）：
cmake --build native/topos_codec/build/release --target topos_codec
ctest --test-dir native/topos_codec/build/release --output-on-failure -j8   # 84 项（随批递增）
".venv/bin/python" -m pytest tests/native tests/shared tests/media -q        # 随批递增（必须含 tests/media——V 收纳结项复查教训）
# 批 0 spike 复现（工具已落地，约 10 分钟）：
".venv/bin/python" tools/bench_lp_spike.py   # → tools/lp_spike_2026-09-14.json
```

**必读文件**（按序，行号为 2026-09-13 实测）：
1. 本文 §1（产品与码率账）、§2（当前代码事实——全部带行号证据）、§4（GPU 流水兼容性，
   帧间专属，别的计划没有这一章）
2. `native/topos_codec/src/bitstream/frame_header.c:48-147`——版本规则单点（ADR-C027 D-8）；
   `:123-126`（V2+ 对 frame_type/gop_id/ref_distance 的强制零校验，**V9 要解除的第一个点**）；
   `:482-487`（解码接受清单 major 1..8，**加 9**）
3. `native/topos_codec/src/codec/codec.c:274-319`——`cfg_to_frame_header` 的
   `reserved[0]` 低字节版本选择器（**em=10 → V9 加分支处**）；`:380-430`（cfg 域校验，
   reserved[0]∈{0..9} → **{0..10}**）；`:4624-4863`（enc_band_job 按 (major,em) 分派）；
   `:7633-7832`（dec_slice_job 同）
4. `native/topos_codec/src/mov/mov.c:734-743`——stss **当前全帧标 sync**（V9 改只写 I）；
   `:1173-1191/:1667-1690`（读侧 stss 位图，缺失=全 sync 的安全默认）；`:2123-2131`
   （`tc_movie_packet_sync`，**nearest-previous-sync 查询不存在，要新增**）
5. `src/shared/codec/topos_profiles.py:41-86`（ToposProfileTier 结构）、`:96-218`（六档 tier
   表，LP 加行处）；`native/topos_codec/docs/capability_manifest.json:128-201`（**先改
   manifest 再实现是仓库规则**）
6. `src/shared/media/topos_source.py:713-725`——`seek_frame` 当前"仅移动游标"语义
   （V9 seek 改造核心落点）；`src/shared/codec/topos_binding.py:713-720`（ToposDecoder
   持久 context——**GOP context 的现成设计模板**）

**关键既有资产（V9 直接继承，不重写）**：
- 熵层：V8 段化多链 rANS 全套（段=16 块独立流 + 瓦片共享 350B 表 + 段目录 12B；
  `v8_encode_plane` codec.c:3632-3860、批 3 CPU 参考解码、golden_v8 conformance）。
  V9 P 帧残差 = 同一套 token→段流机器，只换预测来源。
- 残差数学：颜色管线本就是 signed 域（`bd_mid` 电平偏移后 int16 输入变换，
  transform.h:3-14 输入域 |x′|≤2047）——**帧间残差（±2047@12bit）与该域恰好重合**，
  变换/量化/熵通路零新增数值代码（§3.3）。
- 重建共享路径：`color_store.h:1-12`（反量化+IDCT+钳位+直写，编解码共享保证逐位一致）
  ——P 帧重建 `ref+residual′` 在同文件加一路即可。
- Alpha：独立第 4 平面恒走 MED+signed 残差+Rice（slice_codec.c:38-39），与熵版本正交
  ——天然满足"颜色 P + Alpha 逐帧 intra"策略（§3.7）。
- 解码调度：tpool 常驻池（TC_SLICE_MAX_THREADS=16）、slice band 粒度任务、scratch 槽位
  复用、稳态零分配（rd3_05）。
- 测试方法学：差分机（C↔kernel↔oracle 三方位位精确）、golden/conformance 体系、
  fuzz corpus 按版本追加。

---

## 1. 立项依据

### 1.1 产品定位与码率账（Topos 422 LP）

| 项 | 数值 | 说明 |
|---|---|---|
| LT 档（对标画质） | 0.94075 bpp → **234 Mbps**@4K30 UHD | topos_profiles.py:130 |
| **LP 档名义锚点** | 0.60281 bpp → **150 Mbps**@4K30 UHD（18.75 MB/s） | 150e6/(3840×2160×30) |
| 市场锚点 | DJI Pocket 3 最高 150 Mbps、松下 S1/S5 150 Mbps 档（皆 HEVC 长 GOP） | 消费机甜点区 |
| 竞品注脚 | 松下 150 Mbps 档为 4:2:0 8-bit；4:2:2 10-bit 需 200~400 Mbps | **LP 同体积天然 422 10-bit，采样更全** |
| 差距账 | HEVC 长 GOP 每比特效率高于微 GOP；LP 赢面 = 同码率 + 422/10-bit + 剪辑免转码 | **不打"同码率同画质赢 HEVC"这张牌** |

- **卖点排序**：① 拍完直接逐帧剪、跳过代理生成（HEVC 家族的行业痛点，整个市场被迫
  转 ProRes 或等代理）；② 同 150 Mbps 体积下 4:2:2 10-bit；③ IP-4 随机访问代价
  （≤3 帧）对普通用户不可见、对剪辑师可接受——"消费级体积 + ProRes 级剪辑手感"。
- **近期落地不依赖相机厂商采纳**：LP 作为"可剪辑中间档"——HEVC 素材转一次 LP
  （≈一次转代理的成本），剪辑手感对齐 LT、体积约 ProRes 422 LT 4K30（≈353 Mbps）
  的四成。
- 排期定位：TRAW 批 1~5 之后第二梯队；母版/交付档（LT~XQ）**永远 Intra 不受影响**。

### 1.2 为什么微 GOP，不是长 GOP

| | 微 GOP（本计划：IP-2/IP-4） | 长 GOP（明确不做，§10） |
|---|---|---|
| 工程量 | 复用 V8 段熵 + 新 P 语法，批 0~7 ≈ 2~3 周（agent 节奏） | B 帧/亚像素/双向/CABAC 状态机，另一档量级 |
| 随机访问 | ≤1/≤3 帧补解，剪辑手感可保 | GOP 30~60，剪辑体验劣化（正是要解决的症状） |
| 收益 | 20~50% | 3~6×（但对标的是 HEVC 主场，赢不了生态位） |
| GPU 流水冲突 | 可控（§4） | 更深 |

### 1.3 收益与代价先验区间（**非承诺**——批 0 spike 在当前 rANS2 机器上钉死）

P 帧体积比 ρ（16×16 整像素、有界搜索、无亚像素无双向的第一代预测器下）：
静止/屏幕 ρ≈0.15~0.3；一般 B-roll ρ≈0.5~0.7；重颗粒 ρ≈0.8~1.0（回退 I）。

| 场景 | IP-2 体积收益 | IP-4 体积收益 |
|---|---|---|
| 静止/三脚架/屏幕 | 35~45% | 45~60% |
| 一般 B-roll/访谈 | 20~35% | 35~50% |
| 快速平移/手持 | 15~25% | 25~40% |
| 重颗粒/切镜密集 | ≈0（回退保底，单素材不劣 Intra +3%） | 同左 |

- 速度先验：编码 1.2~1.6×（**对冲项：P 帧豁免逐帧 sized qp 搜索**——当前六档全是
  逐帧 sized 搜索（topos_encoder.py:909），P 帧按 GOP 锚 qp 直编后，IP-2 实测可能
  显著低于 1.5×，批 0 一并钉死）；顺序解码 0.9~1.15×（P 帧数据少 20~40%）；
  随机访问 ≤1.75×（IP-2）/ ≤2.75×（IP-4）。
- 修正项：本先验源自 8/30 Rice 基线时代的估计；rANS2/V8 使帧内更强，帧间相对收益
  预期缩水数个百分点——时间冗余是帧内摸不到的正交维度，方向不变。
- **任何数字在批 0 实测前不得写进对外材料**（8/30 计划第 0.7 条纪律原文继承）。

---

## 2. 当前代码事实（2026-09-13 三路调研，全部行号实测）

### 2.1 版本机与帧头：钩子现成

- 版本规则单点 `tc_frame_header_validate`（frame_header.c:48-147）：major 2..8 公共分支，
  V8 唯一合法熵三元组 (8,0,0)（:87-96）。解码接受清单 major 1..8 逐值枚举
  （frame_header.c:482-487）。编码选择器 `cfg_to_frame_header`：`em = cfg->reserved[0]&0xFF`，
  em=9→V8、**其余(含 0)→V1（生产默认就是 V1）**（codec.c:274-319；CLI 默认
  coding_selection=0，encoder_cli.c:50/81-87）。cfg 域校验 reserved[0]∈{0..9}
  （codec.c:380-430）。
- **`frame_type`(byte15)/`gop_id`(byte16-17)/`ref_distance`(byte18) 三字段物理已在
  53B 帧头**（frame_header.h:34-36），写点/读点/序列化齐备（frame_header.c:363-365/496-498），
  但编码器恒写 0（codec.c:246/260）且 **V2+ 与 V1 解码全部强制为 0**
  （frame_header.c:123-126/138-146）——这就是当年为帧间预留的钩子。V9 批 1 解除
  强制零校验 + 激活写点即可，**帧头字节布局零改动**。
- bytes 45-47 = 熵三元组 (entropy_mode, codebook_version, coding_mode)，**不是空闲
  预留**（V7-A 用 (5,5,2)）；byte 48 `reserved_v2_0` 恒 0 强校验（frame_header.c:525-527）。
  V9 = 新 major 窗口，不动这些字段的既有语义。
- 包结构：V8 有 `tc_packet_is_v8/scan_v8`（packet.c:82/95/258）；`tc_packet_parse_structure`
  对 major=8 显式拒绝（packet.c:13-24）。`tc_frame_decode` 探测链
  V7-B→V7-A→V8→legacy（codec.c:8292-8320）——**V9 加一条探测分支**。

### 2.2 编码器真无状态；解码器无参考帧概念

- `tc_frame_encode` 无状态单帧（codec.c:5557-5606）；`enc_shared` 进程级 scratch 只含
  缓冲/水位（codec.c:571-667）；唯一跨帧残留是 qp 搜索种子 qp_hint（codec.c:645-657，
  注释明确"种子不改变结果"）。**参考帧存储必须全新建**（编码器重建帧 + 解码器参考帧，
  各 1~3 张）。
- 解码 `tc_frame_decode` 无状态；持久 context 先例 = `decoder_ctx.c:1-29`（只持
  dec_scratch，"单实例不可重入"）+ 绑定层 ToposDecoder（binding:713-720）——**GOP
  context 照此形状建**。
- 注意缺口：V8 无 sized encode（codec.c:7232-7233）、无 alpha（codec.c:3864-3865）、
  无 scaled decode/reduced（codec.c:8373/8697）——V9 继承 V8 熵层时逐项清算（§5 批 2）。

### 2.3 残差域天然兼容（免电平偏移的关键简化）

- 帧内路径：像素 − `bd_mid`（10bit=512/12bit=2048，codec.c:197）后进变换，输入域
  **|x′| ≤ 2047（12bit）、|F| ≤ 2^25**（transform.h:3-14），量化 signed 幅值制
  （quant.h:40-48），熵侧 `tc_rice_map_signed`（rice.h:31）。
- 帧间残差 = source − pred ∈ **±2047（12bit）/±511（10bit）——与变换输入域精确重合**，
  且以 0 为中心，**连电平偏移都不需要**。残差块的变换/量化/rANS2 token 化全部复用
  现行代码；`TC_TRANSFORM_MAX_ABS_F=1<<25` 防御界原样成立（输入域不变）。
- 重建：`recon = clip(pred + residual′, 0, 2^bd−1)`——在 color_store.h 共享重建路径
  加"参考帧加法"一路，编解码同源保证逐位一致（cctx.mid/max 注入点 codec.c:7724-7725）。

### 2.4 容器 / 绑定 / 源：三处要改的语义

- **stss 全帧标 sync**（mov.c:734-743 循环写 k+1）→ V9 改只写 I。读侧 `parse_stss`
  （mov.c:1173-1191）+ 位图（mov.c:1667-1690）现成，**缺失=全 sync 的默认对旧文件
  天然安全**。**nearest-previous-sync 查询不存在**（全库 grep 证实）——`tc_movie_prev_sync`
  新增（topos_codec.h:672-718 API 区）。
- tpcC version = 轨级 bitstream_major（build_tpcc mov.c:224-249，首包学得 mov.c:457、
  记录 mov.c:842）→ V9 文件自动 tpcC=9；读端接受上限（V8 批 5 现状 ≤8）同步放行。
- Python：编码侧逐帧无状态（ToposCodec.encode → tc_frame_encode，binding:2057-2109；
  sized 变体 :2195）；`ToposVideoEncoder`（topos_encoder.py:289）逐帧
  `_encode_packet`（sized 搜索 :909/:919）+ mux.add_packet（:742-743），abort 语义
  干净（:802-817）。`ToposMediaSource.seek_frame` **仅移动游标**（topos_source.py:713-725，
  docstring 明言"全 intra + O(1) 索引"）——V9 下改为"找前一 I → reset context →
  顺序解到目标"，readahead/平面池/DecodeWorker generation 机制（decode_worker.py:1194/
  :263/:402-447）保留为唯一调度层，**禁止第二套播放线程池**。

### 2.5 性能基线（4K 3840×2160，2026-09 矩阵口径）

| 项 | 数值 | 来源 |
|---|---|---|
| 4K 422 10bit 编码 | proxy/lt/standard/hq = 12.9/14.7/17.2/22.3 ms/帧 | topos_tier_speed_matrix_2026-09-11 |
| 4K 422 10bit 解码 | 7~12 ms/帧（新口径，lt/hq）；qp30 全链 22.8 ms（V8 终口径） | topos_real_footage_matrix_2026-09-12；topos_v8_production_switch §1.3 |
| 1080p lt | 编码 2.4 ms / 解码 1.85 ms | 同上 §一 |
| 编辑器预算 | 4K 25p = 40 ms/帧（现状 422 档 3.3~8.9× 实时，解码不构成瓶颈） | docs/4k-playback-optimization.md §3.1 |
| 帧内存 | 4K 422 10bit ≈ 33 MB/帧 → 参考帧上界 3×33 MB，有界 | topos_v8_editor_integration §2.1 |
| 档位 qp 机制 | 六档皆 bpp 目标 + **逐帧 sized qp 搜索**（实测 qp 轨迹 62~87） | topos_profiles.py:41-92；c043 矩阵观察 3 |

### 2.6 GPU 与跨帧流水：帧间冲突的精确边界

- **rd3_04 跨帧批调度已上产**：批量预取 N 帧时把所有帧的 slice 展平进一次
  `tc_parallel_for`（dec_frames_core_impl，codec.c:8097-8174），4K +5.2%。该结构
  **假设帧序无依赖**——V9 批内必须按 GOP 分段（§4.1）。
- **A2 帧流水 1.57× 是投影、未接线**：串行 22.4ms（0.82×）→ 重叠后 ≈11.8ms（1.57×）
  为 A/B 分项之上的外推；编辑器生产路径 = CPU 解码 + GL 上传。P 帧依赖链与该投影
  不兼容，若未来接线需 GOP 内软件流水重设计（§4.2 登记为约束，不是本计划工作项）。
- **V8 GPU 解码实测劣于 CPU 3.86×**（88.2 vs 22.8ms，自动回退）——V9 解码只押 CPU，
  GPU 解码为 V8 先解决的独立课题。
- A2-4 融合发射（rans2+V7-R2 专属，codec.c:7801-7812）由 dev 捕获开关驱动，非生产
  默认；与 V9 无交集，无需处理。

---

## 3. V9 格式规格（推荐设计；批 1 冻结前可调，改动须回写本文）

### 3.1 版本信令与扩展点（四处，全部有 V8 先例）

| 决策点 | 取值 | 落点 |
|---|---|---|
| 版本 | `version_major=9`，熵三元组 (9,0,0) | frame_header validate V9 规则组 + GOP 字段强校验；解码接受清单 +9（批 1 已落地） |
| 选择器 | `reserved[0]` em=**10** → V9 | cfg 域 {0..10}；em10 时 reserved[3]/[4]/[5]=frame_type/gop_id/ref_distance（批 1 已落地） |
| 包结构 | **V8 同构继承（批 1 冻结）** | `tc_packet_is_v9` 新增；`tc_packet_scan_v8` major 域 {8,9}；V7 扫描入口拒绝 9；frame_decode 探测链 +V9（批 1 已落地） |
| job 分派 | (9, em) 分支进 enc_band_job / dec_slice_job | codec.c enc/dec dispatch（批 2） |

老二进制对 major=9 返回 UNSUPPORTED_VERSION（新 V9 规则入 validate 即自动成立，
测试钉死）；tpcC=9 由首包学得自动带出。

### 3.2 GOP 模型

- `IP-2`（I P，最大 GOP=2）先行；`IP-4`（I P P P）在 IP-2 发布门通过后（批 7）。
  场景切换/回退允许提前插 I，实际 GOP 可短于上限。
- 无 B 帧、无开放 GOP、无跨 GOP 引用、无长期参考。P 帧 `ref_distance=1`，只参考
  同 GOP 紧邻上一张**编码器重建帧**（绝不允许引用源帧——防 drift 的结构性规则）。
- `gop_id` 每 I 帧递增（u16 回绕规则入 spec），P 帧校验与参考一致；`frame_type`/
  `gop_id`/`ref_distance` 三字段解除强制零（frame_header.c:123-126/138-146）并强校验。
- 事务性提交：packet + recon 全部成功才推进 frame/gop/reference；编码失败/取消/
  输出不足时 context 保持上一成功状态（abort 不留参考、不留文件——对齐
  ToposVideoEncoder.abort 现有语义 topos_encoder.py:802-817）。

### 3.3 P 帧数学（§2.3 的格式化）

```text
pred      = reference_reconstructed(x+mv.x, y+mv.y)   # 首版含 zero-MV
residual  = current_source − pred                     # signed，±(2^(bd−1)−1)
coef      = DCT8x8(residual)                          # 无电平偏移；|F|≤2^25 域不变
qcoef     = quantize(coef, P_qmatrix, P_qp)
residual′ = IDCT8x8(dequant(qcoef))
recon     = clip(pred + residual′, 0, 2^bd−1)         # 下一帧参考（编码器侧）
```

- 残差 token 走现行 color token→V8 段流机器（fill→emit→段 rANS），DC 预测状态在
  inter slice 起点清零（skipped 块 DC 定义为 0）。
- **量化策略（v1 冻结）**：I 帧 sized qp 搜索照旧（以 LT 同 bpp 预算为上限锚），
  得 **GOP 锚 qp**；同 GOP 全部 P 帧**免搜索直用锚 qp**（P_qmatrix=I_qmatrix）。
  收益：省 50%（IP-2）逐帧 sized 搜索开销（当前每帧 sized = 多次全编码尝试），
  是对冲编码墙钟的主要缓解；代价：P 帧码率随内容浮动更大。严格 CB（target_bytes
  硬预算 + 逐帧反馈）后补，机器现成（TRAW 计划 §7 同项）。
- 范围/舍入/饱和逐条重推并入 conformance（residual 输入域、|F| 界、量化死区、
  重建饱和在 bd∈{10,12} 两档各出一份 golden）。

### 3.4 宏块、MV 与边界

- 16×16 luma 宏块 → 残差块 4Y+2U+2V（4:2:2 半宽色度）；partial edge MB 用与 intra
  相同的确定性边界填充。
- 预测层级：**P0 = 零运动**（只有 exact Skip 与 zero-MV 残差）——**批 0 拍板后冻结为 P0（ADR-C047）**；原 P1 = 整像素小范围有界搜索（±R diamond，R∈{4,8} 由批 0 数据冻结）**取消**（批 0 MV 探针上界 +0~10% 且横摇为负，未达开工线）→ P2+（阈值 Skip、更小分块）实测后才议。**不做**：亚像素、双向、可变块、加权预测、全局运动模型。
- 4:2:2 首版只允许**偶数水平 luma MV**（chroma dx=luma_dx/2，dy 不变）；MV 引用矩形
  必须落在 coded reference 内，解码器对越界 MV 返回 malformed（禁止 clamp 猜测）。
- candidate 遍历顺序规范化（平台/线程无关，码流确定）；MV 值域 ±R 强校验。

### 3.5 帧语法（**批 1 冻结**：V8 同构包——原三段式 inter slice 草案作废）

**冻结结论（批 1 落地，test_v9_format 钉死）**：V9 包布局与 V8 **逐字节同构**
（53B 帧头 + 8B 扩展头 + 每平面段目录/瓦片表/段流/瓦片 CRC），扫描层零新增
结构分支（`tc_packet_scan_v8` major 域 {8,9}）。帧级差异全部在 53B 帧头：

- `version_major=9`、熵三元组 `(9,0,0)`（唯一合法）；
- `frame_type` ∈ {0=I, 1=P}；`gop_id` 全域 u16；`ref_distance` I=0 / P=1
  （帧头 validate 强校验）；
- **P 帧 = V8 同构编码「残差伪图」**：`X = clip(curr − recon_ref + bd_mid)`，
  经现行 V8 段化熵机器全平面编码（§3.3 域重合，免电平偏移）；
- **无独立 inter slice 语法**：原 mode/mv/residual 三段式草案**作废**
  （ADR-C047 P0 冻结后无 MV 语法；mode 区分无码率意义）；
- **exact Skip 坍缩**：全零残差经正常通路即得 `recon = ref`（批 0 死区探针：
  qp56~72 无 DC 死区；skip floor ≈8KB@4K ≈ I 的 0.4%——独立 Skip 模式无收益）。
  帧级回退（§3.6）在编码器层，不改语法；
- 帧独立 → 每帧 DC 预测状态天然从零开始（§3.3 的「inter slice 起点清零」
  自动成立）。

### 3.6 Skip 与 I 回退

- **exact Skip**：随 §3.5 冻结坍缩为「量化零残差」（无独立模式）；阈值 Skip
  在 rate-distortion 标定后才可议（约束：max abs err + SSE 界 + 多代界）。
- 帧级 I 回退触发：首帧/周期 I/场景切换；参考缺失/失效/gop_id 不一致/取消后恢复；
  P 超预算；P packet ≥ I candidate（P0 双编码择优——P 编码墙钟 ≈ I，批 0 实测）。
  决策与状态机落 `tc_gop_context_encode_frame`（批 2）。
- 回退纪律：可任意帧插 I，**不可静默切换编码语义**；Intra 永远是可用回退路径。

### 3.7 Alpha（颜色 P + Alpha 逐帧 intra）

- alpha 平面继续走现行独立 MED+signed 残差+Rice 逐帧路径（slice_codec.c:38-39 契约
  不变），不引用时间参考——alpha 损坏不污染下一帧，straight/premultiplied 旗标整轨
  固定。**该策略与现行代码零冲突，新增编码工作 ≈0**。
- 连带清算：V8/V9 熵层 alpha 路径 pending（codec.c:3864-3865）——批 2 内接线（颜色
  段流 + alpha Rice 流并行既有 dec_slice_job plane3 分支 codec.c:7655-7706 可直接
  复用）；若超工作量预算，**LP v1 显式 no-alpha capability + preflight 拒绝**
  （禁静默降级），alpha temporal 预测明确不做（8/30 §4.6 继承）。

### 3.8 错误传播与 conceal

- header/依赖错误：整帧拒绝；P slice CRC/语法错误：当前带 reference-copy conceal
  + warning；**任何 concealed I/P 不得安装为有效参考**；同 GOP 后续 P 返回
  REFERENCE_INVALID 直到下一 I；调用方跳下一 sync 点，禁止静默沿用污染参考。
- 解码器 conceal 与安装参考的判定单点化（V8 段级 conceal 机器 + 新参考安装闸）。
- fuzz 必须覆盖：首帧=P、ref_distance=0/>1、跨 GOP 引用、gop 回绕、坏 MV、坏
  section size、丢包/重排/重复、坏参考后恢复、P 链中间截断。

### 3.9 容器（MOV v9）

- stss **只列 I 帧**（改 mov.c:734-743）；无 B 帧 → DTS=PTS，不引 ctts；stts/stsc/
  stsz/stco(co64) 结构不动；FastStart/co64 与新 sync 表一起进 golden_mov_v9。
- mux 校验：首 sample 必须为 I；P 的 gop/ref 链由 mux 从已验证 packet 推导
  （调用方不得单独传 keyframe flag）；profile/tpcC 一致性。**包级 smart-copy v1
  禁用**（非同步点裁切统一 decode→encode，防断引用片段）。
- reader 新增 `tc_movie_prev_sync(movie, sample_index, &sync)`；恶意稀疏/无 stss
  输入有界处理（无 stss=全 sync 默认保持）。

### 3.10 产品档：Topos 422 LP（tier 定义）

| 字段 | 值 |
|---|---|
| tier_id / label | `lp` / "Topos 422 LP"（UI 副标"长播放 · 体积优先"；10 locale i18n） |
| coding | V9（IP-2 起步 → IP-4），**唯一 IP 档**；母版档不提供 IP 变体 |
| 像素 | native_profile 3、yuv422p10le、bd 10、container mov |
| target_bpp | 0.60281（≈150 Mbps@4K30，18.75 MB/s）——**名义标签与预算参考，非硬追** |
| 码率语义 | CQ 型（TRAW §1.1.1 同款拍板）：I 帧 sized 锚（LT 同画质优先）+ P 帧锚 qp 直编，体积随内容浮动；体积验收线 = 与 LT 等质量（§7） |
| UI 分组 | 体积区 = Proxy、LP；画质区 = LT/Standard/HQ；444 区 = 4444/XQ（LP/LT 一字之差，UI 必须带副标防混） |
| 改动链 | topos_profiles.TIERS → capability_manifest（**先改**）→ deliver 面板 → video_export_formats → i18n → 一致性门测试全链（test_capability_manifest.py 自动钉死） |

- 出货节奏：批 6 以 IP-2 出 LP（名义 ≈165 Mbps 量级、浮动）；批 7 IP-4 落地后同
  tier_id 升级到 ≈150 Mbps 锚。产品话术统一"≈150 Mbps 甜点区（随内容浮动）"。
- capability：`tc_query_capabilities` 加 coding-mode flag 位 + `tc_query_support`
  扩展（topos_codec.h:81-139），manifest/tiers/binding 三方等式入既有门测试。

---

## 4. GPU 管线兼容性（帧间专属章节）

### 4.1 rd3_04 跨帧批的退化与保留

现状：N 帧全部 slice 展平一次并行（codec.c:8097-8174）。V9 依赖链下改为
**按 GOP 分段**：同 GOP 内 I→P 串行（P 需前一帧 recon finalize）；**不同 GOP 之间
无依赖，仍可并行/同批**（I(k+1) 与 P(k) 可重叠）。v1 实现取保守档（V9 帧退化为
逐帧批，损失跨帧批收益中 IP 帧占比的部分，4K 上界 −5.2%×P 帧占比），GOP 级并行
作为批 7 优化项回收。顺序播放编辑器主路径（readahead 逐批）本就按序消费，体感
不受影响。

### 4.2 A2 帧流水投影：登记为约束

0.82×→1.57× 的跨帧 CPU/GPU 重叠投影建立在帧独立上；V9 P 帧剥夺该投影。处置：
该流水本就未接线（§2.6），非生产行为；未来接线时 GOP 内需软件流水重设计——登记
为 V9 对 A 轴的约束条款，不在本计划内解决。

### 4.3 解码只押 CPU

V8 GPU 解码实测 3.86× 劣于 CPU（88.2 vs 22.8ms，自动回退已上产）——V9 解码性能
验收只对 CPU 口径设门（§7）；GPU 解码是 V8 遗留独立课题（链交错 ILP/LDS 驻留，
见 topos_v8_gpu_batch4 优化路径），V9 不等待、不阻塞其上。

### 4.4 编辑器语义（批 5 主体）

- 顺序播放：context 持 ref−1 则直读下一帧（零额外成本）；readahead batch 按 §4.1
  分段。
- seek/scrub：`seek_frame` 改"找前一 I（tc_movie_prev_sync）→ reset decoder
  context → 顺序解到目标（≤1/≤3 帧）→ 只返回目标，中间帧入**每 source 有界 GOP
  缓存**"；generation 变化/取消/换片/close 时 reset/free；坏参考跳下一 I 或结构化
  报错，**不得展示旧帧冒充新帧**。
- 反向播放：cache miss 时按 GOP 正向解码填充有界缓存。
- 剪辑语义：ripple trim/split/replace/代理切换/relink 不得改变可见帧内容；输出
  帧号/PTS 精确不变；proxy/render cache 可选 IP-2，**母版/调色中间默认 Intra**。

---

## 5. 批次分解（每批独立提交 + 落档 + 全量门禁；工作量 = agent 节奏）

**总估计：批 0 半天~1 天；批 1~7 各 1~3 天；合计 ≈ 2~3 周。**

### 批 0：LP spike（零内核改动，立项评审材料）—— **✅ 完成（2026-09-14）**

实测：`tools/bench_lp_spike.py` → `tools/lp_spike_2026-09-14.json`；报告
`topos_lp_spike_2026-09-14.md`；拍板 **ADR-C047**（只做 zero-motion IP-2，
批 4 P1 取消，批 7 IP-4 降级可选，LP 批 6 按 §7 实测评审预期 experimental）。
关键数：全库 LT 锚有效中位 ≈6~11%（<20% 门）；静止/屏幕/切镜类 16~35%；
运动类 ρ>1 由双编码择优回退保底（切镜帧 ρ=1.35 实测回退按设计工作）；
MV 上界 +0~10% 且横摇为负；P 编码墙钟 ≈ I；无 DC 量化死区；
漂移链 PSNR ≥54 dB。

- `tools/bench_lp_spike.py`（仿 bench_traw_pilot.py 模式）：真实素材分层
  （静止/慢移/平移/手持/切镜/颗粒/屏幕 UI）。P0 zero-motion 模拟**完全不动 codec**：
  帧 N−1 按锚 qp 编码→解码得 recon；`X = clip(curr − recon + mid)`（合法 intra 输入，
  §2.3 域证明）→ 同 qp 编码得 P_size → 解码回 `residual′` → `recon′ = clip(recon+residual′)`
  进下一迭代（天然含 drift 链）。
- 度量：I/P 体积比 ρ 分内容层、Skip 率与纯 Skip 帧体积下界（静态内容天花板）、
  1/3/5/10 代漂移曲线、P vs I 编码墙钟（含 sized 搜索豁免效应）、Python ±4 SAD
  原型对运动素材的增益上界（决定 P1 是否值得做）。
- **决策门（M1 gate）**：IP-2 中位 ≥20%？zero-motion 是否足够出货？运动搜索 R 取值？
  双编码 vs 预判？——全部由本批数据拍板，写入 ADR-C011 类决策档。
- 产出：`docs/codec/topos_lp_spike_<date>.md` + json。**收益不足时合法结论 =
  只做 zero-motion IP-2 或停止项目**（旧计划 M1 门槛原文）。

### 批 1：V9 格式冻结 + context 骨架 —— **✅ 完成（2026-09-14）**

- §3.1 扩展点落地（frame_header V9 规则组 + GOP 强校验 / cfg 域 {0..10} +
  reserved[3..5] GOP 输入 / `tc_packet_is_v9` + scan_v8 major 域 {8,9} /
  decode 探测链 +V9 查询可扫）；§3.5 帧语法冻结为 **V8 同构**（原三段式
  inter slice 草案作废，见该节）。
- `tc_gop_context` 骨架（decoder_ctx.c 形状 + 事务状态机 + observe 序列
  校验，零像素重建；feed/encode_frame NOT_IMPLEMENTED）；C 侧 V9 encode/
  decode 像素入口 NOT_IMPLEMENTED 占位；`TC_ERR_REFERENCE_INVALID`（-17）
  入 ABI（§3.8 语义）。
- fuzz 骨架：`fuzz_v9.c`（合成 V8 包头部补丁成 V9 + 变异/截断定义集不变量
  + 查询/实解码契约钉死）；单测 `test_v9_format.c`（头部矩阵/cfg 域/扫描/
  占位/GOP 状态机/截断）。老二进制拒绝 major=9 由「新读端 UNSUPPORTED_VERSION
  语义族 + fuzz 定义集」覆盖（老二进制无 V9 规则组，天然拒绝）。
- 验收全过：不做图像重建即可完整 scan/query GOP；任意截断不越界/不死循环/
  不大分配；V1..V8 全部 golden 校验零变化（conformance ctest 全绿）；
  ctest 81/81 ✓；pytest tests/native+shared+media 3558 passed/65 skipped ✓。

### 批 2：zero-motion IP-2 端到端 —— **✅ 完成（2026-09-14）**

实现走 §3.5 冻结的 V8 同构路线，**零新增像素内核**（原计划的 color_store
参考加法一路、三段式 inter slice 均随规格冻结不再需要）：

- 编码：`tc_frame_encode` major9 直连 V8 像素机（I=源帧 / P=残差伪图）；
  `tc_gop_context_encode_frame` = 残差合成（X=clip(curr−ref+mid)）+ I 回退
  （force_intra / 参考不 READY / **P ≥ 最近 I 自动回退**，免每帧双编）+
  事务提交（BUFFER_TOO_SMALL 不推进状态，参考槽双缓冲 swap）。
- 解码：`tc_gop_context_feed` = 序列校验（几何一致闸防越界）+ I 直解 swap
  参考槽 / P 解残差后 out=clip(ref+X′−mid) 就地推进；坏包 → REF_INVALID →
  同 GOP 后续 P REFERENCE_INVALID → 新 I 恢复。
- 无 alpha capability（create 拒绝，§3.7 允许路线）；recon 编解码同源
  （编码器重建 = 同一 v8_frame_decode + 加法，逐位一致由构造保证）。
- golden_v9（golden_v9_v1.bin 入库 + conformance_v9）：I,P,P,I 序列重编码
  逐字节一致 + 帧型/gop 序列（1,1,1,2）+ **零漂锚**（参考精确重复 →
  recon 逐位不变）；test_v9_codec（零漂/回退/事务/STATE/异几何/能力面）。
- 验收全过：ctest 83/83 ✓；pytest 三目录 3558 ✓；ASan/UBSan/TSan 新测试
  9/9 ✓（san-asan/san-ubsan/san-tsan 新构建目录）；OOM 门（unit_oom）✓；
  SIMD 三路差分未触发（V9 零新增 SIMD 内核，复用已差分的 V8 机器）。

### 批 3：MOV v9 + CLI/inspect —— **✅ 完成（2026-09-14）**

- 容器（mov.c）：mux 逐 sample 同步位（V9 从已校验包头推导帧型——调用方
  不传 keyframe flag）+ GOP 链校验（首 sample 必 I / P gop 一致，与 GOP
  context 状态机同构）；stss 只列 I（全同步轨布局逐字节不变，V1..V8
  golden 零变化）；faststart 经读端位图展开保留 stss 语义（含 co64 升级）；
  读端 tpcC 接受上限 8→9；包解析入口 V9 走 scan_v8 同构分支。
- 新 API `tc_movie_prev_sync`（§4.4 seek 定位：≤ index 最近同步；命中自身
  返回自身；无 stss 默认全同步返回自身；index 前无同步 → TC_ERR_STATE）。
- CLI：decoder_cli V9 GOP 感知路径（prev_sync 定位 → context 顺序解 →
  只输出 start 起；REFERENCE_INVALID 跳下一 sync，禁陈旧帧冒充）；
  probe_cli V9 轨级 GOP 摘要（frames/I/P/first_gop_id）。
- golden_mov_v9_v1.bin 入库（golden_v9 check 扩展）：重 mux 逐字节 +
  sync=1,0,0,1 + prev_sync=0,0,0,3 + faststart moov 前置后包/sync 不变 +
  从每个 I 起解 + P 直启 MALFORMED。
- 验收全过：ctest 83/83 ✓；pytest 三目录 3558 ✓；CLI 实测——`--start 2`
  （P 样本）自动回退 sync 0 解到目标，输出与整解**逐字节一致**；
  ASan/TSan/UBSan 三路新路径 ✓。原『CLI `--gop intra|ip2` 编码开关』项
  取消：编码器 GOP 决策在 context（批 2），CLI 直连入口只承载 I（P 无
  参考语义），无需 flag。

### 批 4：有界整数运动估计（P1）—— **❌ 取消（2026-09-14 ADR-C047：批 0 数据未达开工线）**

批 0 MV 探针（±4/±8 全分辨率 SAD 上界口径）：运动层增益 +0~10% 且横摇为负，
先验 ρ0.5~0.7 不可达。**不达开工线，按预注册纪律取消**；MV 语法字段按保留
零值处理，语义启用须新 ADR + 版本预算评审（ADR-C046）。

- 批 0 冻结的 SAD/candidate 顺序/early termination/MV 语法/chroma 映射/I 回退预判；
  ±R diamond（4:2:2 偶数水平 MV）。
- 验收：scalar deterministic + golden；边界/奇数尺寸/最大 MV；搜索开销与压缩收益
  达 M1 gate——**不达标则保留 zero-motion 并不发布 motion mode**（旧计划 M4 门槛）。

### 批 5：绑定 + 源 + 编辑器顺序链

- 绑定层 GOP context wrapper（显式 close/reset/cancel，仿 ToposDecoder）；
  ToposVideoEncoder 持 sequential encoder（abort 不提交参考不留文件）；
  ToposMediaSource seek/read/GOP 缓存改造（§4.4）；DecodeWorker/generation 兼容。
- 验收：顺播/随机 seek/连续拖拽/反向/并发 source/同 source 串行/cancel/close/
  reopen/损坏 GOP 恢复全测；不新增调度器；RSS 收敛（含 3×33MB 参考槽上界公式）。

### 批 6：422 LP 产品档 —— **✅ 评审完成（2026-09-14）：出货门不达，不注册产品档**

- §7 门评审输入 = 真实现复测（`tools/bench_lp_rho_native.py` →
  `tools/lp_rho_native_2026-09-14.json`，履行批 2 复测义务）：真 inter
  通路优于 spike 代理——静态 **+47%**（vs 35%）、切镜 **+42%**（vs 22%），
  回退保底无一劣化超 +2.1%（≤ +3% 线）；
- **全库中位 ≈ +2~13% < 20% 门** → 按 §7/ADR-C047 裁决：LP 不注册
  （不加 tier/UI/i18n），V9 维持 manifest `v9_micro_gop` experimental；
  评审报告 `topos_lp_gate_review_2026-09-14.md`。

- §3.10 全改动链（manifest 先行）；deliver 面板 + 体积/画质区 UI 分组 + i18n；
  preflight（非 V9 能力环境显式拒绝，禁静默降级）；proxy/render cache opt-in 接线。
- 验收：deliver/quick export LP 全链导出往返；manifest 三方等式门绿；LP vs LT
  等质量口径（PSNR/SSIM 带）达 §7 线；时间线真实导出→解码像素比较。

### 批 7：IP-4 + 压力矩阵 + 发布评审 —— **不执行（2026-09-14 结项裁定）**

IP-2 未达 20% 线 → IP-4 无独立收益来源（ADR-C047 降级成立）；无产品档
则无发布评审对象。批 2/3 已交付的压力面（fuzz_v9 变异+截断、GOP 状态机
坏包恢复、损坏 GOP 序列测试）随批入库；引擎保持可重启状态（重启仅需
产品面注册，见批 6 报告 §4）。

- IP-4（只改 max GOP 与 seek/cache 上限，**禁止顺带加新运动工具**——旧计划 M10 纪律）；
  GOP 级跨 GOP 并行回收（§4.1 优化项）；压力矩阵（场景切换/高运动/噪声/重复帧/
  渐变/长文件/gop 回绕/OOM/磁盘满/随机 seek）。
- **IP-4 启动条件**：IP-2 在真实项目完成一次发布候选浸泡，P0/P1 全关，且没有靠
  加大缓存/隐藏错误/降画质换指标（旧计划原文）。
- 验收：任意目标最多 I+3P；seek/reverse/cancel/坏第 1/2/3 张 P 全覆盖；IP-2 行为
  不变；全门禁绿 → 发布评审（含对外话术冻结：数字全部实测口径）。

---

## 6. 测试矩阵（摘要；完整条目随批落 conformance/fuzz corpus）

- **native unit/conformance**：SAD/candidate 序/tie-break/偶数 dx；残差变换域界与
  量化舍入；mode/MV/section size/mask；context 生命周期与事务提交；IP/II/IPI/IPIP/
  提前 I/gop wrap；scalar↔SIMD、1/4/8/16 线程 parity（码流不随线程数变）；bd 10/12
  双档 golden。
- **fuzz**：单 P packet 解析、合法/非法序列状态机、首帧 P、错 gop/ref、缺帧/重复/
  重排/截断/位翻转、超长码字、越界 MV、MOV stss/tpcC 与链不一致、encoder config/
  scene cut/cancel；corpus 保留 V1..V8 全部旧 seeds。
- **应用集成**：import/probe/play/seek/scrub/reverse/cache/close/reopen；generation
  取消与代理/源切换；Timeline/Composite/Deliver/RenderCache 真实输出；CPU/GPU frame
  ready/present 分段计时；RSS 收敛。
- **素材分层**：静止/慢 pan/快 pan/缩放/手持/运动模糊；hard cut/fade/闪光；肤色/
  天空/文字 UI/渐变；胶片颗粒/传感器噪声；奇数宽高/非 8 倍数/1080p/4K/短长文件；
  10-bit 域全值测试数据（禁 `<<2/<<4` 上采惯性——TRAW 批 5 教训条目沿用）。

## 7. 验收 gate（允许成为推荐档的门，不是预先保证）

| 维度 | gate |
|---|---|
| 体积（等质量口径，proxy 素材库中位） | IP-2 ≥ **20%**；IP-4 ≥ **30%**；任一素材回退后不劣于同配置 Intra **+3%**（容器开销除外）；分内容层报告，**不对全素材宣传单一数字**。**批 0 实测锚（ADR-C047）：全库中位 ≈6~11%，预期不达 → 批 6 LP 以实现后实测评审，不达标保持 experimental/UI 隐藏；对外话术仅限「静止/屏幕类 16~35%、全类型不劣 +3%」** |
| 画质 | decoder/encoder recon bit-exact；LP vs LT 等质量（Y/U/V PSNR+SSIM 带，批 6 冻结阈值）；多代零漂移；alpha 维持现行误差契约 |
| 编码速度 | IP-2 编码墙钟 ≤ Intra **1.50×**（含 GOP 锚搜索豁免后实测口径） |
| 解码速度 | 顺序播放总解码 p95 ≤ Intra **1.15×**；随机 decoded-frame-ready p95 ≤ **1.75×**（IP-2）/ **2.75×**（IP-4）；index/read/I 解码/每 P 解码/GPU 上载/first-present 分段报告 |
| 内存 | context 创建时按 geometry 硬上限（参考槽 4K ≈ 33MB/帧 ×≤3）；取消/seek/close 后 RSS 收敛；禁按码流声明无界分配 |
| 确定性 | 同输入同配置下线程数不改变 packet；candidate 顺序跨平台一致 |

未达标 → 功能保留 experimental、UI 默认隐藏，不得宣称"剪辑手感无差别"
（旧计划 §10.3 原文）。

## 8. 回归纪律（每批提交前）

1. 三命令门禁全绿（ctest 84+、pytest 718+，随批递增）。
2. **V1..V8 全部 golden SHA 零变化是硬门禁**；V9 改动只做加法，触碰共享原语时以
   旧位流位精确测试为安全网。
3. golden_v9 / golden_mov_v9 每批重跑比对；fuzz corpus 增量保留。
4. 每批独立提交（`feat(v9): 批 N——…` 中文摘要含数据与判定）+ 落档 `docs/codec/`
   + 本文档批次状态行更新。
5. capability manifest 三方一致性（先改 manifest 再实现——仓库规则，门测试自动生效）。

## 9. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 跨帧批收益退化（rd3_04 −5.2% 上界） | 解码墙钟 | GOP 级并行回收（批 7）；顺序播放体感不受影响（本就按序消费） |
| A2 帧流水投影（1.57×）与 P 链冲突 | 未来 A 轴上限 | 未接线、非生产行为；登记为约束条款（§4.2） |
| GPU 解码期望落空（V8 3.86× 劣化先例） | 规划失焦 | V9 只押 CPU；4K 422 解码 7~12ms ≪ 40ms 预算，CPU 口径已达标 |
| 版本矩阵复杂度（生产默认 V1 + V8 窗口待拍板 + V9） | 维护面 | LP 显式绑定 V9；V8 生产切换拍板独立进行不受影响；版本机 (major,em) 分派为既有模式 |
| P 帧 sized 搜索豁免导致码率浮动超预期 | 名义锚失真 | 批 0/2 实测浮动区间；名义话术"≈150Mbps 量级"；CB 严格预算后补（机器现成） |
| V8 熵层 alpha/sized 缺口连带 | 批 2 范围蔓延 | 批 2 内逐项清算；超预算则 LP v1 显式 no-alpha capability + preflight 拒绝（禁静默降级） |
| stss 语义改动回归 | seek 崩坏 | 旧文件无 stss=全 sync 默认保持；prev-sync 与只写 I 同批落地（批 3 绑定验收） |
| 损坏 P 污染后续 GOP | 画质事故 | 事务参考 + REFERENCE_INVALID + conceal 不入参考（§3.8）+ fuzz 矩阵 |
| 双编码开销超预期（P0 择优） | 编码墙钟 | P 编码本身便宜（批 0 实测）；P1 改廉价预判；不达 gate 保留 zero-motion |
| 重颗粒素材收益归零 | 话术 | 回退保底 +3% 上界；分内容层报告（§7）；LP 定位含"随内容浮动" |

## 10. 明确不做（本窗口范围外）

- B 帧/双向预测、亚像素、可变分块、加权预测、全局运动模型、CABAC 类状态机；
- 长 GOP（>4）与"对标 HEVC 3~6×"的消费录制药方（生态接线/HW encoder 是另一立项）；
- TRAW 帧间（RAW 固有 intra 假设，TRAW §7 已锁定）；
- 母版档（LT~XQ/444 家族）的 IP 变体——LP 是唯一 IP 档，母版/调色中间永远 Intra；
- 包级 smart-copy/remux（v1 统一 decode→encode）；
- inter 的 GPU 编码/解码（ME GPU 化、GPU 解码均为独立课题）；
- Alpha temporal 预测（独立 profile 能力，需独立参考/回退/golden）；
- 严格 CB 码控（target_bytes 硬预算 + 逐帧反馈；后补选项，机器现成）。

## 11. 成功标准（立项时冻结，结项对照）

1. 422 LP 档出货：等质量验收（§7 画质线）达标，名义 ≈150 Mbps 量级（浮动话术），
   deliver/quick export/时间线导出全链绿；
2. 体积中位：IP-2 ≥20%、IP-4 ≥30%（素材库等质量口径），单素材回退不劣 +3%；
3. 速度：编码 ≤1.50×、顺序解码 ≤1.15×、随机访问 ≤1.75×/2.75×（分段报告齐）；
4. 正确性：recon bit-exact、线程不变量、多代零漂移、损坏恢复语义一致
   （REFERENCE_INVALID → 跳 sync，无陈旧帧冒充）；
5. 兼容：V1..V8 golden SHA 零变化 + golden_v9/golden_mov_v9 冻结入库 + 老读端
   UNSUPPORTED_VERSION 干净拒绝；
6. 编辑器全链：import/play/scrub/reverse/export/proxy/cache/close/cancel 通过，
   不新增调度器，RSS 收敛；
7. 文档不含任何未经批 0/批 7 实测的体积/速度承诺数字。

## 12. 结项复审（2026-09-14，全量代码复查）

对已交付的批 2/3/5 实现做整体复查（gop_context.c / codec.c V9 分支 /
frame_header / packet / mov.c / 绑定 / 编码器 / 源），发现并修复 9 处问题。
**共同根因**：批 2 测试内容（±4~±5 小扰动）在锚 qp 下全部量化归零，
导致三类本应暴露缺陷的断言（零漂锚 / fresh-ctx 确定性 / BTS 事务）在
「残差恒 0」的退化形态下全部空转通过。

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| 1 | Critical | `tc_gop_context_encode_frame`：recon 推进参考发生在 `out_cap` 检查**之前**——BUFFER_TOO_SMALL 返回后参考已被一个「解码端从未收到的包」的重建污染，重试的 P 残差针对错误参考计算，解码输出错位一帧 | out_cap 检查前移到 recon 之前（事务全量不提交）；回归：BTS 重试包与干净历史逐字节一致（test_v9_codec） |
| 2 | Critical | feed 只在解码**返回码**非 TC_OK 时置 REF_INVALID，而 V8 像素机对坏瓦片 CRC 是 conceal 继续（返回 TC_OK）——坏 CRC 的 P 包以隐藏垃圾瓦片**静默安装为参考**，后续 P 全链漂移（违反 §3.8；§9 风险表声称的缓解当时并未实现） | feed/encode recon 对 `concealed_slices>0` 按 MALFORMED 拒绝 + REF_INVALID；测试：毁瓦片流中段一字节 → MALFORMED → REFERENCE_INVALID → 下一 I 恢复 |
| 3 | High | `m7_prepare`（AQ/RDO F-cache 写端）无 major 8/9 闸：em=10+reserved[1]=1 经 `tc_gop_context_encode_frame` → `tc_frame_encode` 直达 m7_final，产出 **major=9 帧头 + legacy Rice payload** 的坏包（self-check 拦为 MALFORMED，编码硬失败）；产品触发路径 `aq_mode='on' + gop='ip2'`（ip2 清 v2/rans2 但不清 aq/rdo）| C：m7_prepare 对 major 8/9 早退（fcache 不适用，确定性降级 AQ/RDO 为关——与 token 缺失回退同语义，也顺带修复既有 V8+AQ 同洞）；Python：ip2 与 aq/rdo 构造期显式 ValueError；测试：AQ/RDO 开关输出逐字节一致 + ValueError |
| 4 | High | `tc_frame_encode_sized` 拒 em=9 却放行 em=10——qp 搜索可静默产出 V9 包（绕过 I/P 决策与残差语义的未审计写口，违反「写端口径审计」纪律） | em=10 → NOT_IMPLEMENTED（V9 唯一写入口 = GOP context；测试覆盖） |
| 5 | High | golden_v9 / test_v9_codec 在**同一 ctx 上交错 encode+feed**：encode 的 recon 与 feed 的重建各推进参考一次 → P 残差应用两次（共享链输出 = 真解码器输出 + 残差再叠一次）；因测试内容量化归零而不可见 | 解码链建模改独立 dctx；契约写入 topos_codec.h（单链语义：交错调用双重应用，解码链须独立实例）；golden_v9 重生成**逐字节不变**（两种构造下 F2 残差同为全零） |
| 6 | Medium | 源 `_v9_read_next` 每次 miss 都 abort+全量重解，且缓存保留**已消费方向**（sync..target−1）——顺序前进永不命中，IP-2 每帧平均重解 1.5×、IP-4 达 2.5×，前缀平面全量分配进内存 | `_gop_pos` 位置追踪：顺序前进单帧 O(1) feed；miss 路径前缀帧 feed 无 planes（零像素分配）；缓存 ≤1 帧；seek/坏包恢复语义不变（现有跨 GOP seek 逐位一致测试通过） |
| 7 | Low | fuzz_direct 的 NULL 平面探测提前 return——实解码分支为死代码（I 包从不会被像素解码 fuzz） | 探测调用删除，小几何检查先于分配 |
| 8 | Low | fuzz_direct 平面超限时直接 return——已分配平面泄漏（libFuzzer leak 检测误报源） | 全部平面先过尺寸检查再分配，统一释放 |
| 9 | Low | 测试内容校准：零漂锚/确定性断言依赖「残差恰好量化归零」——锚强度不足 | test_v9_codec 重构：±60 每 3 样本非零残差（断言 dec_a≠dec_b 证明过量化门）+ F2=解码输出锚（残差恒 0 构造性成立，不再依赖量化行为） |

门禁：ctest 83/83；san-asan / san-ubsan 下 v9_format/v9_codec/golden_v9/fuzz_replay 全绿；
pytest 三目录 3564 passed / 65 skipped；**V1..V8 与 V9 全部 golden SHA 零变化**；
工作区只含本表修复（10 文件）。遗留观察（不改行为）：`tc_frame_decode_reduced`
非 FULL 路径对 V9 包经 legacy 扫描器拒为 UNSUPPORTED_VERSION（错误码口径与
request 路径的 NOT_IMPLEMENTED 不一致，硬拒绝语义正确，登记为后续清理项）。
