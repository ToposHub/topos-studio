# Topos Image HALF（float16）样本域 —— 设计记录（2026-09-19）

> 规范正文见 `topos_image_file_spec_v0.md` §15（冻结）；能力登记见
> `capability_manifest.json` profiles."4"。本文记录决策过程与实测依据，
> 供后续演进（视频侧 float 档、GPU 消费端接线）引用。
>
> **v1.8 产品命名更新（ADR-C057，2026-09-21）**：预设弃 `hf-*`/HF 词，
> 改为四档质量阶梯 `float-ultra/high/medium/low`（qp44/58/72/82，锚点
> 即本文 §实测表），TMET 展示名 `toos <Quality> 16bit float`；真无损
> 不入阶梯（`--half --qp 20` 显式达成）。下文的 hf-lossless/hf-N 档名
> 为当时的历史命名。

> **2026-09-28 复验补充**：四个 `float-*` 有损产品档对 Inf/NaN
> 采取写盘前拒绝。合成探针发现有损量化可将 Inf 或 NaN 改成有限值；
> 因此下文关于映射双射及特殊值的逐位性质只适用于明确满足无损条件的
> 显式 HALF/固定 QP 路径，不能外推到有损产品预设。有限值的误差梯度
> 与负零行为见 `../codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md`。

## 1. 动机与需求

- .toos 图片格式增加 16 位浮点存储模式：**压缩方式与既有 TOPOS 编码完全
  一致**，仅样本类型为 half（IEEE 754 binary16）；
- 用途：HDR 线性合成中间件 / 精确缓存——half 值即样本本身（负值、>1.0
  高光、次正规均合法），无 [0,1] 满刻度契约与 OETF 假设；
- 现有编码速度优势（整数管线 + SIMD bit-exact）原样继承。

## 2. 核心决策

### D1：host 侧冻结映射 front-end，codec 内核零改动

bitstream spec §2.4 禁浮点进入任何规范步骤；bd=16 整数管线（批 4 已
交付）原样复用。half 样本经单调映射（§15.1）转 u16 码值后进编码器——
「压缩方式完全一致」由此结构性保证，而非重新实现。

**为什么不让 DCT/量化直接吃浮点**：违反位流规范、全部 SIMD bit-exact
证明与熵域证明需重做、major bump——与需求自相矛盾，否决。

### D2：sign-magnitude → 对称偏移二进制（symlog 域）映射

$$\text{code} = 0x8000 \pm \text{magnitude bits}$$

- 有限值严格单调（码值序 == 浮点值序）；
- ±0 → 码 0x8000 == bd16 level-shift 中值（浮点零 ↔ 整数零对齐）；
- 全域双射（±0 归一除外）→ qp≤10（Q=1）**逐位无损**，含 Inf/NaN 载荷；
- 码域近似对数幅值 → 码域均匀量化误差 ≈ 浮点域恒定**相对**误差（与
  感知方向一致）；
- 备选否决：线性定标（×2^k）上限 65504 与精度不可兼得且破坏负值；
  原始位模式直存非单调（level shift/DCT 语义崩坏）。

### D3：image_profile=4（Image HF）+ sample_kind=1 激活

- 规范 §6 早已预留 `sample_kind` 值 1 = HALF 与 `half_float` 能力位——
  本次仅激活，无任何版本字段变更；
- 交叉规则双向钉死：profile 4 ⇔ HALF ⇔ pf2/bd16/profile5 ⇔ RGB/4:4:4/
  valid=container=16/transfer=linear(8)；alpha 仅 mode1；
- 旧读端按 reserved 枚举干净拒绝（UNSUPPORTED_VERSION），无误读路径；
- 码流本身是合法 bd16 GBR 整数流——不识别 HALF 的工具按整数解读得到
  单调码值图（可逆误解，不损坏数据）。

### D4：qp 语义（实测，2026-09-19，96×64 混合内容）

| 区间 | 结论 |
| --- | --- |
| qp 0–10（Q=1） | 逐位无损；**内容须在 token 域内**：平坦 DC 码值偏移 0x3000 可编 / 0x3100 拒绝（DC 增益≈10922，|Δdc|≤2^27−1）；±0x4400 级值域噪声的实现依赖尾部会越界；越界 = 显式 INVALID_ARGUMENT + "raise qp"（不静默损坏） |
| qp 11–19 | flat 1.0 最小可编 qp=12；混合内容 qp≥11 全部可编且实测逐位往返（量化误差 <0.5 码 → 重建舍入精确） |
| qp ≥20（Q≥8） | 全内容可编（native 批 4 注释钦定的设计工作地板）；qp 11–36 实测逐位无损，qp 48 出现 ±2 half-ULP |

产品默认 **qp=20**（编码器既有默认），严格归档 qp≤10 + 内容域约束。
α 通道（mode1）同语义。

**测试顺序教训（记录给后来者）**：token 域判定对噪声**实现**敏感——
测试平面生成必须用独立固定种子（不共用全局 mt_rand 流），否则用例
通过与否取决于前面测试消耗了多少随机数。端到端用例内容收敛在码值
±0x2C00 中心域。另一陷阱：`0x8000 + 负偏移` 的无符号回绕会落进
0x7Cxx/0x7Fxx（±Inf/NaN 位模式区）——half 位模式必须按「幅值+符号位」
构造。

## 3. 交付清单

| 层 | 内容 |
| --- | --- |
| native | `src/image/half_map.{h,c}`（映射 + 批量 C ABI）；`image_container.c` HALF 校验/交叉规则；`image_reader.c` 能力位（half_float=1、profiles bit4）+ derive_idsc profile 4；CMake 接入 |
| 单测 | `tests/unit/test_half_map.c`：golden/全域双射/单调/批量一致/IDSC 规则 8 正反例/qp0 端到端逐位无损（含 alpha、含 Inf/NaN spike）/确定性拒绝负例 |
| Python | `topos_image_binding.py`：numpy 映射（与 native 全域互钉）、`ToposImageInfo.sample_kind`、`decode_float()`；`topos_encoder.py`：pix_fmt `gbrph16le`/`topos_gbraph16a16`、float 直通转换（无裁剪/无 [0,1] 契约）、构造器交叉校验（4444 档、mode1 alpha、拒整数输入）、transfer 冻结 linear、`_write_image_packet` → image_profile 4 |
| 测试 | `tests/media/test_topos_half_float.py`（10 用例）；`test_image_container.c`/`test_topos_image_source.py` 枚举激活断言更新 |
| 文档 | spec §15 + §6/§9 修订；capability_manifest profiles."4" + half_float=true |

native ctest 86/86；pytest media/native/export 3185+ 通过（预存的
Metal bridge / V8 环境失败与本次无关，已用旧库基线对照甄别）。

## 4. 后续（未含在本批）

1. **GPU 消费端**：`yuv_gpu_upload.py` 已有 R16F 通路；接线「HALF 文件 →
   decode_float → R16F 直传」分支（渲染端 RGBA16F 工作纹理惯例现成）；
2. **视频/序列 float 档**：MOV 侧无 IDSC——需 tpcC/track 元数据扩展
   （sample_kind 轨级声明），映射与 qp 语义直接复用；
3. **UI 档位**：`topos_profiles.py` tier 声明 + 导出选择器暴露
   gbrph16le；
4. **质量基线**：真实 HDR 素材（EXR 半浮点源）的码率/PSNR-in-float 基线，
   并入 benchmark_protocol；
5. **SIMD 映射**：当前批量映射为标量循环；64K 项 LUT gather 或
   pshufb 可再提速（编码热点 profiling 后按需）。

## 5. 追加（同日晚）：真实视频帧基准 + 质量档位 + CLI 缺陷修复

### 5.1 2K 视频帧实测（tests/test_videos，1920×1080，各抽 1 帧）

ProRes422HQ / DNxHR444 10-bit / H264 → RGB48（ffmpeg）→ [0,1] float16 → G,B,R 平面。
完整表见 spec §15.3。要点：

- **qp20 / qp32 在全部三帧上逐位无损**（1.1–1.6:1）；
- **qp44 视觉透明**（92.7% 像素逐位一致，PSNR ≈82 dB，±2 half-ULP），且
  **体积（6054 KiB）小于同帧 EXR half+zip16（6568 KiB）**；
- 有损代理档到 102:1；
- 无损档大于 EXR zip16——DCT 无损对视频传感器噪点不如 zip 预测器；这是
  内容特性而非格式缺陷（合成/渲染平滑内容 DCT 占优，96×64 合成 28:1 无损级）。

### 5.2 质量档位（CLI --tier，对齐 raw12/16 家族命名）

`hf-lossless`(qp20) / `hf-2`(qp44) / `hf-4`(qp58) / `hf-8`(qp72) / `hf-16`(qp82)，
锚点=5.1 实测；`--tier hf-*` 蕴含 `--half`；`--qp` 显式值可覆盖锚点。

### 5.3 顺带发现并修复的 CLI 预存缺陷

- **`toos encode` 违反 ABI 契约**：无 alpha 时给 `input.planes[3]` 赋了
  one-past-end 指针（契约要求 i ≥ plane_count 的槽位为 NULL）。编码器在
  V7-R2 路径会消费该指针 → 内容相关地产生损坏码流（且随二进制布局漂移，
  极难排查）。修复：只填 `nplanes` 个指针（`image_cli.c`）。
  native 单测/Python 绑定一直正确传 NULL，故 86/86 与 pytest 均未暴露。
- 排查中留下的教训（记录给后来者，含我自己）：基准脚本三次翻车分别因为
  ① `astype('<u2')` 是数值转换不是位模式重解释（应用 `view`）；② 通道序
  R,G,B 与 G,B,R 平面契约错位；③ **`np.stack(..., axis=-1)` 是逐像素交错
  布局，不是平面布局**（平面拼接应用独立 tobytes 或 axis=0 再 reshape）。
  三次都产生了"编解码器损坏"的假象，实际编解码器全程正确。
