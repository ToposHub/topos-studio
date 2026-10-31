# TRAW 格式落地计划（Topos RAW：topos_traw_format_plan）

日期：2026-09-12 · **档位拍板 2026-09-13（位深×比率 6×2 全网格 raw12-*/raw16-*，无 Lossless 档；档位=CQ 质量锚定、比率为定位标签、码率随内容浮动，见 §1.1.1）** · 状态：**批 0~5 全部完成（2026-09-13）**——批 1（8dae6115）、批 2（3366f545）、批 3（9b3d91a9）、批 4 阶段 1（6dc7d124）/阶段 2（02f7f6bf 内核 + 锚点标定/产品面）、批 5 计划点名项（b3e7ef31）+ 16-bit 产品面。批 4 阶段 2 要点：i32 前向 AVX2/NEON 三路差分、SIMD 域门控（wide resolve）、编码端系数预失真（16-bit 无损 maxerr=0 的数学保证）、熵验收域按位深外推、raw16 六档锚点实测回填。遗留项见各批『后续（P4/样张依赖）』
前置实测：`tools/bench_traw_pilot.py` → `tools/traw_pilot_2026-09-12.json`（批 0 产物）；
批 1 验收：`tools/bench_traw_batch1.py` → `tools/traw_batch1_2026-09-13.json`（pf=3 产品链六档实测）
关联：ADR-C030（V7-B 共享变换金字塔）、ADR-C021（MOV 元数据）、ADR-I011（图片档位）、
`topos_v8_format_plan_2026-09-12.md`（V1 minor 代际门控先例）、
**ADR-C046（位流代际生命周期与退役规程，2026-09-13）——V7-B 为保留代，
本计划的 frame_header/version 触碰须遵守其退役闸门与保留代保护条款**
适用读者：**全新会话的执行 agent**——本文档自包含，按 §0 启动。

---

## 0. 新会话引导（先读这节）

**项目**：Topos Color 图像/视频编辑器，自研 topos_codec（C 库 + Python 绑定）。
本计划 = 在现有 codec 上增加 **TRAW（Topos RAW）**：Bayer 相位平面 RAW 编码档，
对标 BRAW（log 制作档）/ CinemaDNG（linear 归档档）。**不动既有 V1..V8 位流语义**。

启动 checklist：

```bash
cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01"
# 构建/测试（三个命令就是全部门禁）：
cmake --build native/topos_codec/build/release --target topos_codec
ctest --test-dir native/topos_codec/build/release --output-on-failure -j8
".venv/bin/python" -m pytest tests/native tests/shared -q
# 批 0 试点复现（约 2 分钟）：
python3 tools/bench_traw_pilot.py
```

**必读文件**（按序）：
1. 本文 §1（实测立项依据）与 §2（位深战略结论）
2. `native/topos_codec/docs/ADR-C030-v7b-shared-transform-pyramid.md`（V7-B 能力边界）
3. `native/topos_codec/src/bitstream/frame_header.h`（TPIC 53B 布局：byte 10 profile、
   byte 12 bit_depth、byte 34 color_transfer、plane_coded_w[4]）
4. `native/topos_codec/src/bitstream/frame_header.c:173-206`（V1 minor 代际门控——
   bd/pf 枚举扩展的唯一合法路径）与 `src/codec/codec.c:233-242`（写侧 enc_fh_select_minor）

---

## 1. 立项依据（全部为实测，非估计）

批 0 试点：**不改动 codec 内核**，用现有 toos CLI（GBR 4:4:4 12-bit + rans2 + qm0）
承载 RGGB 相位平面，UHD 3840×2160（真实素材 4K_prores422HQ #10 + 合成场景），
传感器仿真（shot+read 噪声，base ISO read=5e，σ_Gr≈99e），PWL-log 12-bit 曲线
（线性趾 x<8e + 11 档 log 至 FW=16384e）。

### 1.1 压缩率阶梯（真实素材，4 平面真实账 = s1 + s2/3）

| qp | 压缩率 | 码域 maxerr | 噪声裕量 | 定位 |
|----|--------|------------|----------|------|
| 36 | **1.62:1** | 0（数学无损） | 25.1dB | 无损档 ≈ FFV1 锚（1.64:1）**打平** |
| 44 | 2.03:1 | 1 码 | 25.1dB | 近无损 |
| 52 | 2.74:1 | 2 码 | 20.1dB | 透明（误差 10× 低于噪声） |
| 58 | **3.73:1** | 4 码 | **15.4dB** | **透明拐点（推荐 Log 标准档）** |
| 64 | 6.07:1 | 12 码 | 7.8dB | 近透明（交付代理） |
| 70 | 17.6:1 | 31 码 | 0.5dB | 极限代理（噪声底被侵蚀） |

- 噪声裕量 = 20·log10(σ_sensor/σ_codec)；≥15dB 时总 SNR 损失 <0.12dB，不可见。
- qm1 探针：qp52 = 4.05:1 但裕量仅 8.1dB——透明区权衡劣于 qm0 高 qp。**TRAW Log 冻结 qm0**。
- 合并 G 参照（3 平面、绿采样减半）仅 2.2:1 → **采用 4 相位平面方案**（Gr/Gb 不合并）。
- 高 ISO（read=25e）全表与 base 几乎一致（噪声主导域）→ 档位定义跨 ISO 稳定。
- 曲线自身量化：p99 相对误差 0.092%（真实素材）——比传感器噪声低两个数量级，曲线不可见。

### 1.1.1 产品阶梯（拍板 2026-09-13：位深×比率 6×2 网格，无 Lossless 档）

12-bit（log 编码，制作档）六档，批 1 出货；16-bit（linear 编码，归档档）六档
同构，批 4 出货。比率分母统一 = 16-bit 容器（W×H×2）→ 同比率跨位深**名义**
体积相同，菜单网格尺寸对称；实际体积随内容浮动（CQ 语义，见下方拍板项）。

| 产品档位（12-bit） | tier id | 实测锚点 qp | 比率 | 噪声裕量 | 验收下限（典型素材） |
|---|---|---|---|---|---|
| Topos RAW 12-bit 2:1 | `raw12-2` | ~45 | 2:1 | ~25dB | ≥20dB |
| **Topos RAW 12-bit 4:1（默认）** | `raw12-4` | ~59 | 4:1 | ~13dB | ≥12dB（透明线） |
| Topos RAW 12-bit 6:1 | `raw12-6` | 64（实测） | 6:1 | 7.8dB | ≥6dB |
| Topos RAW 12-bit 8:1 | `raw12-8` | 66（实测） | 7.7:1 | 5.0dB | ≥4dB |
| Topos RAW 12-bit 12:1 | `raw12-12` | 68（实测） | 10.9:1 | 2.4dB | ≥2dB |
| Topos RAW 12-bit 16:1 | `raw12-16` | ~69.5（内插 qp68=10.9 / qp70=17.6） | 16:1 | ~1.2dB | 无下限（代理档） |

16-bit 网格（`raw16-2` … `raw16-16`）与上表同构，锚点标定随批 4（linear 域
噪声特性不同，qp 锚点需重标，比率与码控机器同一套）。

- **Lossless 档移除**（2026-09-13 拍板）：市场先例缺失（BRAW/RED/ProRes RAW
  均无无损档）+ 实测价值有限（1.62:1）；无损语义由 16-bit linear 归档
  **模式**（批 4）承载——它是模式不是档位。
- **档位语义 = 质量锚定（CQ 型，2026-09-13 拍板修订）**：比率档名只是用户
  识别的定位标签，实际码率随内容上下浮动——各档为固定锚点 qp 预设（无码控
  机），画质下限（上表验收列）是硬约束，体积在画质保证前提下尽量小。RAW
  文件体积由传感器噪声熵主导：干净场景（棚拍/低 ISO/CG）自动显著小于名义
  比率，噪重场景略大。严格比率码控 CB（target_bytes 硬预算）为后补选项
  （§7），机器现成。
- 阶梯几何步距均匀（2×/1.5×/1.33×/1.5×/1.33×）；高端码控易命中
  （qp68→70 比率 10.9→17.6，噪声门限区 qp 微调空间大）。

### 1.2 速度（UHD 每帧，4 平面口径，CLI 单线程墙钟）

encode ≈ 143~180ms（≈6 fps），decode ≈ 98~107ms（≈10 fps）。离线/ ingest 可用；
解码侧已有 GPU rANS2 底座（V8 链），批 2 GPU 化后帧率目标另立。

### 1.3 结论

1. 现有机器承载 Bayer 相位平面**成立**：无损与 FFV1 打平、透明 3.7:1、曲线不可见。
2. 与 BRAW 宣称 5:1+ 的差距归因于 8×8 块变换 vs 全帧小波——**批 3（V7-B 金字塔做
   base 层）是追赶路径**，不阻塞批 1。
3. log-12 的"无损"是**码域无损**（曲线已不可逆量化，高光 ~30e/码）；16-bit 域真无损
   只能由 Linear 模式提供 → 双模式结论（§2）。

---

## 2. 位深战略结论（12 / 16 / 双模）

**结论：双模式——12-bit log 制作模式 + 16-bit linear 归档/无损模式**
（档位命名用位深×比率 `Topos RAW <bd>-bit <N>:1`，见 §1.1.1/§3.3；Log/Linear
词汇用于格式描述）。

| | RAW Log（12-bit） | RAW Linear（16-bit） |
|---|---|---|
| 对标 | BRAW Q 系 / ProRes RAW | CinemaDNG / FFV1 归档 |
| 域 | e- → PWL-log 12-bit 码（曲线冻结 §3.2） | 传感器原生线性整数（≤16-bit ADC 直存） |
| 无盟能力 | 码域无损（曲线量化不可见但不可逆） | **16-bit 域数学无损** |
| 内核前置 | **无**（批 1 纯接受域扩展） | 批 4 内核加宽（int16 残差溢出三处硬阻塞） |
| 落地 | 先行（批 1~3） | 后置（批 4~5） |

**与视频/图片 16-bit 的关系（"顺带做完"成立，且顺序唯一正确）**：
16-bit 内核批（§4 批 4）一次性解锁三个产品面——TRAW Linear 无损、视频 16-bit 档、
图片 16-bit 档。三处硬阻塞（`intra.c:152-161` int16 残差 cast、`transform.h:14`
|F|≤2^25 域、`color_store.h:342-360` SSE2 packs 饱和）是三者共用的同一批内核工程；
TRAW Linear 是该批次的需求驱动方（单独为视频/图片 16-bit 立项动机不足，
三面共用摊薄成本）。批 4 前视频/图片 16-bit 一律不做。

---

## 3. TRAW 格式规格（推荐设计；批 1 冻结前可调，改动须回写本文）

### 3.1 核心决策

1. **pixel_format = 3（CFA）**，4 相位平面 R/Gr/Gb/B（各 W/2×H/2）。
   帧头 `plane_count=4` + `plane_coded_w[4]` 几何现成；CFA 平面全是"类亮度"平面，
   无色度相位问题。**AQ（色度逐带 qp 偏移）对 pf=3 关闭**（单色平面无色度语义）。
2. **profile = 7（TRAW）**（帧头 byte 10；白名单 {3,5,6} → +{7}，7..255 仍空闲）。
   交叉规则：profile 7 ⇔ pf=3 且 bd ∈ {12（Log）/16（Linear）}。
3. **曲线 id 存帧头 byte 34 `color_transfer`**（字段现成，扩枚举）：
   新值 TC_TRANSFER_TRAW_LOG0 = PWL-log12（参数 §3.2，冻结）。
4. **版本演进走 V1 minor 代际门控先例**（bd=12 当年路径）：新 minor 代 = pf=3 ∧
   profile=7 枚举扩展；旧读端 UNSUPPORTED_VERSION 干净拒绝，**不 bump major**。
5. **开发元数据**（WB RG/BG gain、EI/ISO、传感器色矩阵、flat 域）：MOV 走
   ADR-C021 元数据原子扩展；.toos 静态帧走 IDSC 保留区/扩展段。解码器不解释
   元数据语义（透传），debayer/ISP 由应用层消费。
6. **.toos 图片侧**：image_profile 新值 3（RAW），`valid_bit_depth` 白名单
   {8,10,12} → +16（`container_bit_depth` 枚举已含 16）；图片 tier 菜单新增
   `raw-log`（批 1）与 `raw-linear`（批 4 后）。
7. **档位与码控**（批 1 冻结，源自 §1.1.1 拍板）：12-bit 六档
   `raw12-2/4/6/8/12/16` = 质量锚定预设（qm0 + 各档锚点 qp 参照 §1.1.1，
   固定 qp 无码控机，体积随内容自然浮动）；比率档名仅作定位标签；默认档
   12-bit 4:1。16-bit 六档 `raw16-*` 同构占位，随批 4。无 Lossless 档
   （无损由 16-bit linear 模式承载，批 4）。锚点为自研机器实测效率点
   （非对标竞品比率；无损域与 FFV1 打平 1.62:1 vs 1.64:1 为效率硬证据），
   仍需 DNG 样张 ≥3 组复核（批 1 验收项）。

### 3.2 PWL-log12 曲线（冻结参数，与批 0 试点一致）

```
输入 x = e- 域线性值（0..16384）；输出 c = 12-bit 码（0..4095）
log 段（x ≥ S）：  c = A·log2(1 + x/S)，A = 4095/log2(1+(FW−S)/S) ≈ 372.27
线性趾（x < S）：  c = c0 + k·x，k = A/(ln2·2S) ≈ 33.57，c0 = A − k·S ≈ 103.7
参数：S = 8e（趾/锚点），FW = 16384e（设计满阱，log 段覆盖 11 档）
```

解码逆映射为精确算术（浮点实现按位一致；C 参考与 Python golden 同源）。

### 3.3 容器与档位命名

- MOV：tpcC profile=7；编解码三元组 `(pf=3, bd=12/16)` 经现有
  `tc_frame_config_validate` 单一事实源，容器层零改动。
- 产品命名：**位深×比率双数字**——`Topos RAW <bd>-bit <N>:1`（如
  `Topos RAW 12-bit 4:1`）。两个数字即全部决策要素：位深是相机行业母语
  （12-bit ARRIRAW / 16-bit REDCODE），比率是 BRAW 母语；数字 locale 零翻译。
  模式 ≡ 位深：12-bit = log 编码制作档、16-bit = linear 线性归档档（Log/Linear
  词汇降级为格式描述与营销页用语，不进档位名）。
  排版规则：**位深必带 `-bit`、比率必带 `:1`**——"16-bit 16:1" 同框零歧义。
  UI 副行显示换算数据率（≈ MB/s · GB/分钟 @ 当前分辨率）——RED 放弃比率命名
  转卖 MB/s 的教训。位深是 profile 级结构属性（pf=3+bd+transfer），tier id
  编码位深前缀（raw12-*/raw16-*）。同比率跨位深名义体积相同（标称口径统一
  16-bit 容器），实际随内容浮动（CQ 语义）。
  视频线与 RAW 线互引用 bits/px 对齐（视频线 5~70:1，RAW 线 2~16:1，互补
  不重叠）。

---

## 4. 批次分解（每批独立提交 + 落档 + 全量门禁）

### 批 0：可行性试点 —— **✅ 完成（2026-09-12）**
`tools/bench_traw_pilot.py` + `tools/traw_pilot_2026-09-12.json`。四问已答：
压缩率阶梯（§1.1）、透明拐点 qp58=3.7:1@15.4dB、Gr/Gb 拆分代价（采 4 平面）、
速度（§1.2）。本文档即立项产物。

### 批 1：RAW 12-bit 档（无内核改动）—— **✅ 完成（2026-09-13）**
- frame_header.c：profile 白名单 +7、pf 白名单 +3、bd 交叉规则（pf3⇔profile7）、
  color_transfer 枚举 +TC_TRANSFER_TRAW_LOG0（=20，FFmpeg trc 空间自由区）、
  minor 代际门控加一代（v1.6，minor=5）+ `enc_fh_select_minor` 写侧。
- 平面语义修复（pf=3 致命点，审计驱动）：`plane==3 → alpha` 分派三处
  （enc_band_job/dec_slice_job/slice_map k3）改 alpha_mode 感知；
  derive_geometry 增 CFA 半宽半高推导；qm/qp_delta/RDO λ 的色度判定改
  `is_chroma_plane`；AQ 对 pf=3 强制关闭；sized 码控对 pf=3 显式拒绝
  （§7 后补选项）；V8/V7-A 编解码入口对 pf=3 显式拒绝。
- intra.c / version.c / image_container.c / image_reader.c / packet_synth /
  image_file_synth / inspect / topos_image.h 同步；image_profile 3 改派
  Image RAW（原 reserved Lossless 值复用）+ TC_IMG_CHANNEL_MODEL_CFA。
- toos CLI：raw12-2/4/6/8/12/16 六档质量锚定预设（qm0 + 锚点 qp，无码控接线）
  + CFA 4 平面输入输出 + probe/sidecar 通道名 R,Gr,Gb,B；MOV mux 批 1 末接入
  （encoder_cli 仍未出）。
- golden：`golden_codec_v1_traw.bin`（V1-Rice + rans2 双熵 6 用例，conformance
  新测试 conformance_codec_traw）+ `golden_image_v1_traw.bin`（.toos 信封）；
  单测：test_frame_header v1.6 段（正例 9 断言 + 交叉反例）、test_codec TRAW
  往返段（几何/确定性/质量界/sized 拒绝）、test_image_container RAW 组合；
  capability manifest 双份同步 + validate_topos_image_profiles 扩展（先改
  manifest 再实现——仓库规则）。
- 验收（tools/traw_batch1_2026-09-13.json，UHD 3840×2160 real+synth）：
  **六档噪声裕量全部达 §1.1.1 下限**——raw12-2=25.0dB（≥20）、
  **raw12-4（默认）=4.02:1@14.3dB（≥12 透明线，比率命中名义 −0.5%）**、
  raw12-6=7.8dB（≥6）、raw12-8=5.0dB（≥4）、raw12-12=2.4dB（≥2）、
  raw12-16=18.3:1@0.5dB（无下限）；与批 0 试点锚点同域吻合（qp64/66/68 三点
  裕量逐位复现）。体积浮动记录：real 素材 −12.6%~+8.7%、高频合成素材
  +1.4%~+43.1%（CQ 语义符合预期：干净/低频显著小于名义，噪重略大）。
  门禁：ctest 85/85（+conformance_codec_traw）、pytest tests/native+shared+media
  全绿、V7/V8 既有 golden SHA 零变化。
- 遗留：DNG 真实 RAW 样张 ≥3 组复核锚点——仓库暂无 DNG 样张，样张就位后
  复跑 `tools/bench_traw_batch1.py`（输入适配 DNG→相位平面）复核。

### 批 2：编辑器侧（应用层，可与批 1 并行启动）—— **✅ 完成（2026-09-13）**
- 序列源放行 pf=3：`topos_cfa12le` 格式注册（frame.py）、
  ToposImageSource/Sequence 白名单 + Image RAW profile 名、
  `TC_TRANSFER_TRAW_LOG0` 进 ColorTransfer 枚举与色彩语义解析（GBR/CFA
  identity 契约合并）、read_frame 帧尺寸取 IDSC 声明值（CFA 平面半尺寸）
  + extra 透传 `topos_cfa`/`cfa_pattern`（解码器透传契约）；CFA 恒全尺寸
  解码（preview 缩放路径未承载 pf=3，随批 3 金字塔接入放开）。
- GPU debayer v1：`src/shared/video/cfa_gpu_debayer.py`——4×R16UI 纹理 +
  双线性 RGGB debayer（texelFetch 边界 clamp）+ LOG0 逆曲线（Python 同源
  常量）+ WB gain/EI stops/预览 OOTF uniform → RGBA16F。
- Edit 页接线：`_classify_planar_format` 增 'cfa' + `_upload_and_render_planar`
  CFA 分支（track_fbo 直出）；`RawParams`（clip_params）进 ClipInstance
  （序列化/undo 命令）→ track_info['raw_params'] → debayer uniform；
  EditPropertiesPanel 新增 RAW section（WB×3 + EI + OOTF，拖动零重解码）。
- 测试：tests/media/test_topos_raw_sequence.py（序列源 5 项）+
  tests/video/test_cfa_gpu_debayer.py（GPU vs NumPy 参考逐像素 + 曲线常量
  + WB/EI 语义 + 奇数维 + 尺寸拒绝 7 项）。
- 验收：UHD 3840×2160 GPU debayer 单帧 **8.45ms**（≥24fps 预算 41.7ms，
  超过 60fps 线）——CPU 解码侧约 100ms/帧（批 0 实测），整链实时性受解码
  支配、GPU 环节非瓶颈；WB 调整 uniform 路径像素级验证实时生效。
  门禁：pytest tests/native+shared+media 3547 全绿（native 无改动，ctest
  维持批 1 的 85/85）。

### 批 3：V7-B 金字塔接入（压缩率追赶 + 预览档）—— **✅ 完成（2026-09-13，含一项门控决策）**
- pf=3 使能：base_scale（CFA 全平面半宽半高）、base_encoder（平面数/位深
  alpha 感知）、v7_scalable（plane_count + LOG0 transfer 归一化）。
- 验证（ctypes 经公开 binding + C harness 实测）：
  - V7-B TRAW 编码 OK——UHD 3840×2160 CFA base+残差包 1.55MB（10.73:1）；
    公开 `encode_scalable` binding 端到端通（128×128/64 残差路径生效）。
  - **预览 = 只解 base**：base-only 段跳过生效（UHD：seg_read=1、skipped=4、
    残差字节 7.8KB 不读）；`decode_reduced` 到任意代理网格 OK——BRAW 式
    「一档文件同时喂代理与全画质」的预览/代理面打通。
  - 同几何（base=源）完整解码位还原 maxerr=0 × 4 平面。
- **门控决策**：minor-2 量化残差 FULL 合并路径（源>max_dim 时）在 pf=3/UHD
  下失败/挂起——该路径本处 reference 状态（P4 匹配质量门禁未过，视频默认
  writer 同被门控）。对 pf=3 FULL 显式 NOT_IMPLEMENTED；TRAW 完整画质由
  rans2（V7-R2）直连流承载（位还原已证，批 1）。金字塔（minor-4）压缩
  追赶与视频线同受 P4 门禁，随批后解锁。
- 压缩率复测：直连透明档 4.02:1（批 1 实测）vs ≥4.5:1 目标——差距归因
  8×8 块变换 vs 全帧小波的结构项，追赶路径 = minor-4 金字塔（P4 门禁后），
  本批以 V7-B base+残差包 10.73:1（UHD，含损）验证了结构可行性。
- 已知遗留（批 4 已清）：CFA preview 缩放解码平面几何在批 4 阶段 1
  接线后语义正确（dec_frame_build_jobs CFA 目标半维 + plane_geometry 按
  目标帧，实测 target 64×48 → 平面 32×24）；批 2 的渲染器 debayer 接线
  缺口（_upload_and_render_planar 签名缺 track_info、CfaGpuDebayer 从未
  被调用）随批 4 补完——'cfa' 分类 + 上传分支 + raw_params uniform 消费
  + topos_cfa16le 注册 + CfaGpuDebayer 16-bit linear 变体；序列源 CFA
  跟随 options.target_size（预览提速解锁）。

### 批 4：16-bit 内核加宽（RAW 16-bit + 视频/图片 16-bit 共用批）—— **◎ 完成（阶段 1: 6dc7d124；阶段 2: 02f7f6bf + 锚点/产品面，2026-09-13）**
- 已落地：frame_header bd 白名单 +16 + profile7 位深-传递曲线冻结对
  （12→LOG0/16→linear）+ V1 minor=6 扩展代；**硬阻塞 2**（color_store
  SSE2 packs 门禁回退标量——原路径 16-bit 产出错值）；
  **硬阻塞 3 部分**（|F| 域钳位按位深外推 f_clamp：color_store/quant 全
  钳位点参数化；fastdiv N_MAX=2^25 越域 → bd≥13 标量环真除法 exact_div）；
  **int32 残差双路**（intra forward_residual bd≤12 走 int16 快路逐位不变、
  bd≥13 走 tc_transform_forward_8x8_i32_scalar 规范路）；IDSC valid_bd+16；
  bd16 编码面 config/query 显式 NOT_IMPLEMENTED（融合前向 SIMD 内核为
  i16 域）。门禁 ctest 85/85 + pytest 与基线一致。
- 阶段 2 落地（2026-09-13）：**融合前向 i32**（`tc_transform_forward_8x8_i32`
  AVX2 mullo / NEON vmlaq 内核，bd≤16 全域与 scalar bit-exact，三路差分机
  SCALAR/AUTO/FORCE × 满摆/DC/脉冲对抗块进 unit_transform）；**SIMD 域门控**
  （resolve 系列加 wide 参数：量化 fastdiv n≤2^25 域外 → exact_div 真除法、
  融合反量化/逆变换 AVX2 B 16-bit 分解域 → 宽域标量 compose、forward_rows
  i16 截断 → i32 compose；dec 批入口 per-frame 按 bd 覆写）；**16-bit 无损
  数学保证 = 编码端系数预失真** F'=round(F·2^32/(W·EuEv))（W 表项舍入偏差
  在 12-bit 码域 <1 码不可见、16-bit 满摆 DC 可达 ~9 码——预失真后解码总
  残差 ≤0.008≪0.5，仅编码侧取整自由度、解码器零改动）；**熵验收域按位深
  外推**（slice_codec 三核心 m/DC 域 bd≥16 放行到 rans 字母表结构容量，
  V1/V2-Rice 12-bit 冻结域编码侧显式拒绝）；**raw16 六档锚点实测回填**
  （UHD 合成场景：2:1→qp64 实测 2.28:1 / 4:1→qp76 4.22:1 / 6:1→qp80
  5.90:1 / 8:1→qp82 7.43:1 / 12:1·16:1→qp84 域界 8.60:1——线性域熵天花板
  ≈8.6:1@qe95，标签在当前 8×8 变换 + rans2 字母表结构下不可达，档位保留
  命名一致性；tools/traw_batch4_2026-09-13.json）；**16-bit 产品面**（CLI
  raw16-* 预设 + 图片容器 RAW 组合 bd16、pix_fmt 解析/display 名/tier 声明
  扩 16、encoder 位深域 10/12/16）。
- 无损验收（计划 §批 4）：unit_codec v1.7 TRAW 16-bit linear 段 maxerr=0
  （4 平面 × 满刻度探针）+ tests/media raw16 无损域往返 maxerr=0 + CLI 实测
  qp20-36 全域 maxerr=0（批 0 试点 12-bit qp36 同域结论在 16-bit 复现——
  变换归一化 EuEv 与位深无关）。
- bd_qp_offset bd16 复核：解析式 +12 保持（qm0 flat16 的 Q=1 域到 qp_eff≤22，
  qp_base=0 无损语义不受损；量化误差落到像素域 <1 码的机理与位深无关）。
- 门禁：ctest 85/85（V7/V8 golden SHA 零变化）+ pytest native/shared/media
  全绿（3548+）。
- 真实 DNG 锚点复测（2026-09-13，samples/dng/ 四传感器：DJI 4P 20MP
  16-bit 码域 / Leica CL 24MP / Pentax K-1 36MP / K-3 III 26MP 14-bit，
  tools/bench_traw_batch4.py 直编真传感器码域）：① 锚点终版 =
  qp 51/72/76/78/80/82（合成场景初标 64/76/80/82/84/84 作废——合成纹理
  压缩性远低于真实素材，曾误推『线性域熵天花板 ≈8.6:1』，真实素材实测
  最高 39.7:1 @ qe95，12:1/16:1 档可达）；② **qe 死区修正（2026-09-13
  复查）**：qe=qp_base+12 封顶后可分 qe=[0..63]∪[76..95]，64..75 不可达，
  首版 qp57 系场景分歧（qe63 vs qe76）平均落死区——名义 qe69 从未存在，
  实际被 63 封顶（51..63 字节同径）。锚点融合改有效 qe 域 + 吸附实测格点，
  raw16-2 存 qp51（+12 恰好触顶，自洽可验）；③ 四传感器无损边界一致
  = qp36（1.11~1.35:1，maxerr=0），与 12-bit 试点 qp36 同域交叉验证；
  ④ 码率随内容浮动极值：纹理航拍 7.2:1 封顶 vs 干净画面 39.7:1（CQ
  语义实测支撑）。
- 后续（P4/依赖外部条件）：② 编码端 token 域前置硬校验（当前越域流由
  解码端 conceal 兜底，产品路径不产生）；③ NEON i32 内核在 ARM 硬件上的
  实测差分（x64 交叉编译验证）；④ V7-A band 路径 bd16（12-bit 冻结域，
  产品用 rans2，不阻断）。
- 三处硬阻塞：`forward_residual` int16→int32 双路（bd<13 走现行 int16 快路保持
  12/12 SHA；bd≥13 走 int32 通用路，scalar+AVX2+NEON 三路差分）；color_store
  SSE2 bd≥13 门禁回退标量（**现路径 16-bit 产出错值，必须门禁**）；|F| 2^25 域
  重推 + fastdiv n<2^26 回退验证。
- bd_qp_offset 标定：`bd>=12?4:0` 扩展 16-bit 档（推算 +24，实测定值）。
- 解锁面：TRAW 16-bit（pf=3,bd=16，16-bit 域数学无损验收 + `raw16-2/4/6/8/
  12/16` 六档锚点重标）、视频 16-bit 档、
  图片 16-bit 档（`valid_bit_depth`+16）。
- 验收：差分机 bit-exact；V7/V8 既有 golden SHA 零变化；TRAW Linear 对 16-bit
  整数 RAW 样张 maxerr=0。

### 批 5：产品化收口 —— **◎ 计划点名项完成（2026-09-13）**
- 两处静默降级改显式：`_write_image_packet` 位深黑洞（16→profile2 误标）
  改完整映射表 + 不可映射显式抛错（含 TRAW profile7→Image RAW 映射）；
  `_topos_pixel_format_override` 未知格式回落改显式告警日志（有痕回落，
  默认值场景不破坏）。
- i18n：RAW/预览 OOTF 键补入 10 locale（面板带英文回退，与既有
  opacity 模式一致）。
- 16-bit 产品面收口（随批 4 阶段 2 完成）：`_parse_topos_pix_fmt` 与
  timeline_export planar 正则扩 16、deliver 面板 `_pix_fmt_display_name`
  10/12 硬编码修复（p16/ap16 识别）、hq/4444 tier 声明扩 16（R7 UI 选择域
  自动出现 16-bit 选项）、encoder 位深域 10/12/16；codec_map 默认档位不变
  （16-bit 经既有 R7 覆盖入口选择）。locale 键面无新增（16-bit 选项由格式
  名生成，走既有显示名规则）。
- Python/UI 全链：topos_profiles 新 tier 行、`_parse_topos_pix_fmt` 正则扩域、
  ExportCodec/codec_map、deliver 面板（`_pix_fmt_display_name` 硬编码 10/12 修复）、
  **两处静默降级改显式报错**（`video_export_job._topos_pixel_format_override`
  回落、`ToposImageSequenceEncoder._write_image_packet` 16→profile2 黑洞）、
  10 locale i18n、manifest 三方一致性（守门测试自动生效）。
- 测试域：16-bit 满刻度 65535 = uint16 上限，测试数据不得用 `<<8` 推广既有
  `<<2/<<4` 上采模式，直接生成全域值。
- 验收：deliver/quick export 全档导出往返；capbility 三方等式测试绿。

### 全链复查（2026-09-13 二轮，批 0~5 完成后的全面审计与修复）

三路并行审查（C 内核 / Python 产品面 / 档位一致性）+ qp 封顶语义自查，
发现并修复一批遗漏 bug：

- **raw16 锚点 qe 死区**（标定 bug）：qe=qp+12 封顶后可分 qe=[0..63]∪[76..95]，
  64..75 不可达；首版 raw16-2 qp57 系场景分歧（qe63 vs qe76）直接平均落
  52..63 混叠段（名义 qe69 不存在，实际 63 封顶，51..63 字节同径——
  qp51/57/63 编码字节数逐字节验证相同）。锚点融合改有效 qe 域 + 吸附
  sweep 实测格点，终版 51/72/76/78/80/82；实测区间改为出货锚点固定 qp
  下四传感器口径（名义档位值全部落入区间）；无损边界 qp36 实测比率
  修正为 1.11~1.35:1（此前误引 12-bit 试点值 1.35~1.6）。
- **C 内核三处 Critical**：① `dec_slice_job` 的 `cctx.f_clamp` 未初始化
  （栈垃圾参与 DC-only/稀疏块钳位判定）——按 mid/max 显式推导；②
  bd≥13 熵域门扩展：VLC/C1/C2/V3-intra/V6-range 族（12-bit 冻结解码域）
  此前可配 bd16（VLC 表越界读 / batch-commit ±2^25 逆变换 / AVX2-标量
  intra 模式分歧破坏码流确定性）——收紧为只许 {V7-R/V7-R2/V8}；③
  **token 域前置硬校验（P4 遗留②关闭并升级为内存安全必修）**：bd16 低
  qp（Q<8）+ 满幅密集内容 |level| 越出 rans 字母表容量 → hist/freq 栈
  数组越界写——三个 tokenizer 饱和防越界 + 违例整帧 INVALID_ARGUMENT
  （实测：棋盘 qp0/qp8 干净拒绝、qp26+ 正常、真实 DNG 全域不受影响）。
- **Python 产品面一批 Critical**：16-bit CFA 预览必抛 KeyError 被外层吞成
  静默黑屏（`_LINEAR16_FS_TEMPLATE` 无 LOG0 uniform 但无条件注入——
  改条件注入）；CFA 缩放解码 pf=3 预览缓冲按整帧分配（native 按半维写
  入 → 3/4 零值 + 帧尺寸 2× 错——binding 改 ceil(target/2) 分配 +
  `_resize_for_preview` 同修）；16-bit 视频导出被 binding `movie_config`
  (10,12) 一票否决与 encoder planar 白名单缺 p16（两处闸门按批 4 域
  开放）；`_LINEAR16_FS_TEMPLATE` B 位点邻域抄错（单 Gb + 远点 R 拉链
  伪影——与 12-bit 模板同式修正）；`ToposImageSource` 单帧 CFA 4 平面
  只建 3 个 bit_depths（plane_infos[3] 越界崩溃）；read_frame 缩放后
  width/height 仍取 native 尺寸（三处不一致）。
- **收口项**：TRAW 16-bit 图片序列导出闸门解锁（native IDSC RAW 组合
  已含 pf3+bd16）；MOV→.toos 抽帧映射补 profile7；CFA 轨道 CPU 导出
  显式拒绝（防相位平面按 YUV 乱渲，CPU debayer 列后续项）；debayer
  实例 cleanup/GL 重初始化 release 补齐 + vbo 泄漏；死代码清理
  （timeline_frame_mapper 死 regex、`_MAIN_DEPTH_RE`、`_is_cfa`）；
  奇数维 ceil 几何对齐 native。
- **一致性修复**：native manifest 12bit 六档 measured_ratio/code_maxerr
  回填（批 1 数据在手未回填）；measured_ratio 排版统一 `~`+`:1`；
  docs manifest raw12 note 试点旧值换批 1 实测；RAW profile 位深 [12]→
  [12,16]；traw_tiers 新增测试钉住（结构/跨面一致/可分 qe 域回归钉）。
- 门禁：ctest 85/85（含新增熵域门 + token 前检断言；golden SHA 零变化）
  + pytest native/shared/media/video 全绿。

---

## 5. 回归纪律（每批提交前）

1. `ctest`（release）+ `pytest tests/native tests/shared` 全绿；批 1 起加
   tests/media TRAW 往返。
2. **V7/V8 golden SHA 硬门禁零变化**——TRAW 只走新增枚举/minor 代，不动既有位流。
3. 新链 golden：TRAW 12-bit 位流 SHA + .toos SHA 冻结入 conformance；每批重跑比对。
4. 旧读端拒绝路径：老 decoder/老 image reader 对 pf=3/profile=7 必须返回
   UNSUPPORTED_VERSION（不 crash、不误解码），入 conformance 测试。
5. SIMD 差分：凡触碰内核（批 4），scalar/AVX2/NEON 三路位精确差分先行。

## 6. 风险登记

| 风险 | 影响 | 缓解 |
|------|------|------|
| 透明档 3.7:1 vs BRAW 5:1 起步（块变换结构差距） | 产品话术 | 批 3 金字塔追赶（目标 ≥4.5:1）；若仍不足，定位改为「无损透明 + 生态位」叙事 |
| debayer 画质（双线性放大噪声/伪彩） | 用户可感知 | 批 2 v1 双线性达标即可立项，Malvar/边缘感知列为批 2 后续迭代 |
| 机内 3 平面假设角落（color_convert、AQ、切片并行对 pf=3） | 批 1 实现坑 | pf=3 全链路审计清单（§4 批 1 四处 whitelist 之外的运行时分支） |
| AQ 对 CFA 语义 | 画质回退 | 批 1 冻结：pf=3 关闭 AQ |
| 批 4 内核工作量（三处硬阻塞 + 差分机重建） | 1~2 周量级 | 与视频/图片 16-bit 共批摊薄；Log 档（批 1~3）不依赖批 4，先行落地 |
| 码控锚点素材单一（ProRes 派生仿真） | 档位标定偏差 | 批 1 验收强制 DNG 真实 RAW 样张 ≥3 组复核 |
| 元数据语义范围蔓延（DNG 兼容全集） | 工程失控 | v1 只做 WB/EI/色矩阵/flat 域四项，DNG 全集明确不做（§7） |

## 7. 明确不做（本窗口范围外）

- 帧间/长 GOP RAW（RAW 固有 intra 假设）；传感器子窗口 ROI 采集；
- BRAW/DNG 生态互操作（只做自家闭环 + .toos RAW 静态帧）；
- 12-bit linear 等中间形态；浮点 RAW（EXR half 域）；
- Log 家族 Lossless 档（市场先例缺失；无损由 RAW Linear 模式承载）；
- 严格比率码控 CB（target_bytes 硬预算 + `ToposRateFeedback` 接线；后补
  选项、机器现成——v1 档位为 CQ 质量锚定，比率为定位标签）；
- 批 4 前的任何视频/图片 16-bit 档承诺。

## 8. 成功标准（立项时冻结，结项对照）

1. RAW 12-bit 六档典型素材噪声裕量逐一达 §1.1.1 验收下限（12-bit 4:1 默认档
   ≥12dB；试点同域实测 qp58=3.73:1@15.4dB，新链复测须维持）；体积随内容
   浮动，整体不劣于名义比率定位（干净场景显著更小）；
2. RAW 16-bit 模式：16-bit 整数域数学无损（maxerr=0）；
3. 预览/完整双档（批 3）：预览只解 base，完整位还原源平面；
4. 旧读端干净拒绝 + V7/V8 golden SHA 零变化 + 全量门禁绿；
5. 编辑器内 TRAW 序列 UHD 实时回放（GPU debayer，批 2 验收）。
