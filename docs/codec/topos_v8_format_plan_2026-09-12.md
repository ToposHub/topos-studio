# V8 格式窗口落地计划（topos_v8_format_plan）

日期：2026-09-12 · 状态：**批 0~5 全部完成（结项）**——仅余生产切换拍板项待用户确认（见批 5 结论 / `topos_v8_production_switch_2026-09-12.md`）
前置结论来源：`topos_a3_v8_gpu_rans_2026-09-12.md`（A3/V8 批 1，提交 f8172093）
适用读者：**全新会话的执行 agent**——本文档自包含，按"§0 新会话引导"启动。

---

## 0. 新会话引导（先读这节）

**项目**：Topos Color 图像/视频编辑器，自研 topos_codec（C 库 + Python 绑定）。
本计划 = 给 codec 增加 V8 位流版本窗口（熵层多链化，GPU 解锁），**不动 V7**。

启动 checklist：

```bash
cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01"
git checkout fix-ui && git pull            # 或从 fix-ui 切 feat/v8-format
# 构建/测试（三个命令就是全部门禁）：
cmake --build native/topos_codec/build/release --target topos_codec
ctest --test-dir native/topos_codec/build/release --output-on-failure -j8   # 81 项
".venv/bin/python" -m pytest tests/native tests/shared -q                  # ~670 项
```

**必读文件**（按序）：
1. 本文档
2. `docs/codec/topos_a3_v8_gpu_rans_2026-09-12.md`——批 1 差分机 + 链数规格量化（V8 的全部实测依据）
3. `docs/codec/topos_a2_decode_gpu_2026-09-12.md`——CSR 契约 + GPU 重建管线（V8 解码输出的形状）
4. `native/topos_codec/src/entropy/rans.h`——rANS 原语数值域（V8 直接复用）
5. `src/shared/gpu/metal/kernels_dec.py`——`rans2_decode_slice` kernel（**V8 GPU 解码器的核，已存在**）

**关键既有资产（V8 直接继承，不重写）**：
- GPU：`rans2_decode_slice`（每线程一条 rANS 链，cum 二分替 LUT，conceal 寄存）；
  `inverse_dct_wsd`（CSR → 像素）；MetalComputeContext。
- C：`tc_dev_packet_slices` 导出（packet 级 slice introspection + 同源归一化表）；
  rANS 原语族（`tc_rans_model_build`/`tc_rans2_row_encode/decode`/suffix/rawbits/fastdiv/
  `tc_bitwriter_put_bytes`）；版本机（version_major 1~7 并存分支，`enc_band_job`/
  `dec_slice_job` 按 `(version_major, entropy_mode)` 分派）。
- 测试方法学：差分机（C 捕获 ↔ GPU kernel ↔ numpy oracle 三方位位精确）；
  golden/conformance 体系（V7 位流冻结）。

---

## 1. 立项依据（全部为实测，非估计）

### 1.1 性能模型（AMD RX 6900 XT，4K 3840×2160 422 10bit qp30）

| 链数 | GPU rANS 实测 | 说明 |
|---|---|---|
| 51（V7 默认切片） | 1088ms | GPU 基本空转 |
| 405（**V7 上限** = slice_count ≤ 512） | 169ms | vs CPU 全链路 26ms，慢 6.5× |
| ~2900（外推） | ~26ms | CPU 平价点 |
| ~8000（外推） | ~10ms + wsd 6.6ms ≈ 17ms | **1.5×**，lane 余量还有 20× |

- 链数-时间**反比线性、无饱和**（51→405 链 = 时间 1088→169ms，6.4×/8×）。
- 单 GPU lane rANS 链速 ≈ 14M sym/s ≈ CPU 核的 1/5~1/7（串行依赖链，无乱序）。
- **分辨率收益表**（CPU 随像素线性、GPU 恒定直到 lane 天花板）：

| 分辨率 | CPU 解码（外推） | GPU 解码（外推） | 差距 | CPU 24fps 实时？ |
|---|---|---|---|---|
| 4K | 26ms | ~17ms | 1.5× | 够（38fps） |
| 6K | ~55ms | ~20ms | ~2.7× | 勉强 |
| 8K | ~104ms | ~25-35ms（需 ~6.7 万并发链，逼近 81920 lane） | 3~4× | **不够（10fps）** |

**立项理由锚定 6K+ 时间线**：4K 市场 A2 已覆盖（1.57× 落地）；8K 时 GPU 是
唯一回实时的路径。

### 1.2 码率账（外推自 V7 实测码流体积）

- **共享表设计（本计划采用）**：帧级/瓦片级聚合统计表 + 段引用共享；每段仅
  4B 终态 + ~8B 目录项。合计 **+2~5%**，与分辨率无关（每链簿记按链数涨、
  码流按块数涨、链数∝块数 → 百分比为常数），分辨率越高固定表摊得越薄。
  区间哪一头由内容定：胖码流（噪声/高码流）→ ~2%，瘦码流（平滑）→ ~4-5%。
- **禁止的设计**：每段自带精确表（V7 现式）= 满速粒度下 +18~40%，8K 时表
  字节数超码流本身。**规格里写成硬性条款：表必须共享。**
- 半档 qp ≈ 10% 码率 → V8 代价 ≈ 半档 qp，换 8K 回实时，账成立。

### 1.3 机上探测结论（跨机器部署）

- Metal 查不到"总 lane 数"；可用 `threadExecutionWidth`、
  `maxThreadsPerThreadgroup`、GPU family、`hasUnifiedMemory()`、
  `recommendedMaxWorkingSetSize` 拼出能力画像 + **一次性校准微基准**
  （合成链 kernel 以 1k/4k/16k/64k 线程量斜率拐点，缓存跟机器走）。
- **编码端不需要知道解码 GPU**：链数 > lane 数时按波次排队，每 lane 循环
  处理多条链，只损失亚 1% 簿记。编码端按分辨率过切分即可。
- 弱 GPU 自动回退 CPU 路径（两路并存，校准数据择优）。

---

## 2. V8 格式规格（推荐设计；批 1 冻结前可调，改动须回写本文）

### 2.1 核心决策

| 决策点 | 推荐 | 理由/备选 |
|---|---|---|
| 版本信令 | `version_major=8`，新 `entropy_mode=8` | 复用版本机分派；老二进制明确拒绝 |
| 链粒度 | 段 = 每 `segment_blocks` 块（信号化，默认 16，合法 8/16/32） | 4K 满速需 ~8000 链；按分辨率过切分 |
| 段布局 | **每段独立连续码流段** + 帧级段目录（offset+len） | 备选"交错单流"省目录但复杂；独立段 = 随机访问 + 并行 + 错误隔离，GPU 友好 |
| 概率表 | **瓦片级共享**：每平面每瓦片一套（order-0 起步，ctx 模型二期）；**默认 tile_rows=32 块行，合法 {0,16,32,64} 信号化**（批 0 实测统计损失 ≤0.1%） | 统计损失 = 批 0 实测钉死；禁止每段自带表 |
| rANS 原语 | 逐式复用 V7-R2（L=2^23、SCALE_BITS=12、byte 重整化、suffix/rawbits 旁路） | 差分基础：V7-R2 全套测试直接改造成 V8 oracle |
| DC 预测 | **段局部**（段首块行 has_top=0） | 链独立性的代价已计入统计损失 |
| Q 表 | 段继承瓦片/带 qp；wsd kernel 的 qtab 按段展开 | 现有 qtab 契约不变 |
| CRC | **每瓦片**（不是每段） | 批 1 实测 intro 42-52ms 主因是逐 slice CRC；段级 CRC 会重蹈覆辙 |
| conceal | **段级** CSR 清零 → GPU mid | 比 V7 带级更细（白送的好处） |

### 2.2 帧结构草图

```
[frame header V8]（含 segment_blocks、瓦片表布局、段目录长度）
[plane 0..2]:
  [tile 表 ×T]（每瓦片：dc/run/lvl 归一化表，V7-R2 行格式复用）
  [segment 目录 ×S_p]（每段：stream_off u32 + len u32 + qp_delta u8 ≈ 9-12B）
  [segment 码流 ×S_p]（每段：4B 大端终态 + 数据字节；后向编码产物）
[瓦片 CRC ×T]
```

- 段数上界：`S = Σ_p ceil(blocks_p / segment_blocks)`；4K@16 ≈ 1.6 万段，
  8K ≈ 6.5 万段，12K ≈ 15 万段。目录 12B×S = 0.2~1.8MB（0.6~1.5% 码流）。
- 每段符号预算 @16 块：噪声 qp30 约 1000 对 + 16 DC + 16 EOB——共享表下
  统计无缺口（表来自瓦片聚合，不依赖段内统计）。

### 2.3 GPU 解码器形状（批 4 转正目标）

`rans2_decode_slice` 现核改造：smeta 13×u32/slice → 段目录直读（stream_off/
len/表基址/段基址）；threads=(S,) 每线程一段；表基址指向瓦片表区。
输出 CSR 契约不变（dc plane 稠密 / off 每段 [blocks+1] / pairs 最坏 63×blocks
段拼接）→ `inverse_dct_wsd` 原样接。

---

## 3. 批次分解（每批独立提交 + 落档 + 全量门禁）

**总工作量估计：批 0 半天；批 1-5 各 1-3 个工作日（agent 节奏）。**

### 批 0（前置实验，立项评审材料）：共享表统计损失实测 —— **✅ 完成（2026-09-12）**

- 三段真实素材（建议用 real-footage 矩阵既有素材）× {4K, 8K 缩放} ×
  {qp20, 30, 44}：A = V7-R2 现状（逐切片精确表）；B = 瓦片聚合表模拟编码
  （改 `color_tokens_rans2_encode` 的模型来源为聚合计数，**不动位流格式**，
  只测尺寸差）。
- 产出：统计损失表 → 把 §1.2 的 1~3% 区间钉成单值；若 >5% → 回到规格
  调整瓦片粒度（8→32 瓦片）或段 ctx 模型，重测。
- **验收**：落档 `topos_v8_table_loss_<date>.md`；不合并生产路径（实验代码
  走 dev 开关或独立工具）。
- **结论**：`topos_v8_table_loss_2026-09-12.md`——统计损失可忽略
  （tile=32 块行 → ≤0.1%，整平面 ≤0.9%）；V8 总账由段簿记主导
  （qp≤30：+1.4~2.7%；qp44 瘦源单点 +5.2~5.8% 略超封顶 → 修正路径
  a/b 已登记，批 1 冻结时拍板）。规格回写：瓦片粒度默认 32 块行。

### 批 1：格式冻结 + 版本机骨架 —— **✅ 完成（2026-09-12）**

- 帧头 V8 字段 + 段目录/瓦片表布局定稿（§2.2）；`frame_header.c` 解析/校验；
  旧二进制对 V8 流返回 UNSUPPORTED_VERSION（测试钉死）。
- C 侧 V8 encode/decode 入口占位（返回 NOT_IMPLEMENTED）。
- **验收**：ctest 新增 V8 头部 fuzz/边界用例；V7 全部 golden SHA 不变；
  `topos_inspect` 识别 V8。
- **结论**：`topos_v8_format_spec_batch1_2026-09-12.md`——布局冻结
  （扩展头 8B + 段目录 12B + 瓦片表定长 350B + 瓦片 CRC=表+成员段流）；
  ctest 82/82（新增 unit_v8_format 18 例：版本机/结构扫描/10 变异/CRC
  损伤/入口占位）；V7 入口对 major=8 显式拒绝双钉；inspect 端到端冒烟
  （zlib CRC 与 C 增量 CRC 互证）。

### 批 2：编码器——段化多状态 rANS —— **✅ 完成（2026-09-12）**

- 量化/token 侧：段局部 DC 预测（fill_color_band_tokens 分段变体）+
  瓦片聚合统计收集。
- 熵侧：逐段后向 rANS（复用 `tc_rans_enc`/fastdiv/put_bytes 原语），
  段间独立 flush；目录与瓦片表写出。
- **验收**：4K/8K 各 qp 档 round-trip（配合批 3 的参考解码器）；码率
  vs V7-R2 差值落在批 0 预测 ±1pp 内；golden V8 基准流冻结入库。
- **结论**：`topos_v8_encoder_batch2_2026-09-12.md`——编码器落地
  （串行；段局部 DC + 瓦片聚合 + 逐段后向 rANS，批 0 三原语复用）；
  码率 Δ +1.21~5.04%（18 配置，批 0 预测带内 ±1pp；DC 段局部代价
  +0.1~0.3pp）；golden SHA ×3 冻结；ctest 82/82、pytest 685。

### 批 3：CPU 参考解码器（回退路径）—— **✅ 完成（2026-09-12）**

- 逐段 rANS 解码（复用 `tc_rans_dec`/suffix/LUT-或-二分），段并行进
  tpool；**瓦片表 LUT 只建一次**（禁每段建表——批 1 已知坑）。
- conceal：段级清零。
- **验收**：与批 2 码流 round-trip 位精确（oracle = 编码侧符号捕获差分）；
  CPU 解码 4K 不劣于 V7-R2 超过 5%（表共享正确性的性能证据）；
  差分测试进 `tests/native`。
- **结论**：`topos_v8_decode_batch3_2026-09-12.md`——两阶段并行（瓦片 CRC
  重算 + 段块解码 64 段/任务）+ 瓦片表 LUT 单次构建 + 段级 conceal +
  q-hash oracle（分组无关加法合并）；**4K qp30 解码 29.0ms vs V7 28.8ms
  （劣化 0.7%，门槛 5%）**；q-hash 位精确 4 几何 × 4 qp 全过；
  ctest 82/82、pytest 692。

### 批 4：GPU 解码器转正 + 自适应 dispatch —— **✅ 完成（2026-09-12）**

- `rans2_decode_slice` 段化参数化（kernels_dec.py v2）；CSR 原位接 wsd。
- GPU 能力画像 + 一次性校准微基准（合成链 kernel 斜率拐点，缓存）；
  dispatch 波次 = ceil(S / 有效并发)；校准比较 GPU/CPU 链路自动择优回退。
- **验收**：GPU 全链路 == CPU 参考逐值（4 几何 + conceal 寄存 + 坏段测试，
  模仿 `test_metal_kernels_rans.py` 9 例结构）；4K/8K 基准 vs CPU 落档；
  目标复现 §1.1 外推（4K ~17ms、8K ≤35ms 量级）。
- **结论**：`topos_v8_gpu_batch4_2026-09-12.md`——GPU 全链路位精确 ✓
  （5/5 差分）；**§1.1 线性外推被实测推翻**：段化满占用聚合 ≈1.7ns/sym
  （二分查表 warp 发散 + 重整化访存），链数不改变聚合吞吐——4K GPU
  88.2ms vs CPU 22.8ms，**自动择优按设计回退 CPU**（校准 JSON 落档）。
  优化路径登记：链交错 ILP + LDS 驻留 + 统一内存口径重测。

### 批 5：conformance/工具链 + 生产切换评审 —— **✅ 完成（2026-09-12）**

- V8 golden/conformance 全套（正流 + fuzz + 损伤流 conceal 矩阵）；
  `topos_inspect`/golden 工具/CLI 的 V8 支持。
- 编辑器集成（产品化，单列）：输出直留 GPU 纹理、上载 ring buffer 摊销
  （批 1 实测 17-21ms 分配开销 → 预建后 ~6ms）、CRC 与上载重叠。
- **验收**：全门禁绿；写生产默认切换建议（何时 `reserved[0]=8 → V8`）
  → 用户拍板。
- **结论**：`topos_v8_conformance_batch5_2026-09-12.md`——golden_v8 3 记录
  字节+解码像素双冻结；fuzz_v8 合成路径（512 ctest + 4096 手动，ASan 全量
  84/84）**首跑抓到并修复 v8_frame_decode NULL strides 生产 bug**；损伤
  conceal 矩阵 20 例（瓦片域定界钉死）；工具链补齐（decode_request 实解码
  占位补线、mov 容器 V8 分支 + tpcC ≤8、CLI `--entropy v8`/粒度旗标、
  probe verify 冒烟 3/3）；release ctest 84/84、pytest 718。
  编辑器集成评估单列：`topos_v8_editor_integration_2026-09-12.md`
  （ring buffer 摊销可独立先行；纹理直留/CRC 重叠依赖 kernel 优化追平 CPU）。
  **生产切换建议**：`topos_v8_production_switch_2026-09-12.md`——
  拍板项 1 建议 qp≥40 接受 ≤6%（9B 目录缓行）；拍板项 2 建议**现在不切
  默认**（GPU 劣于 CPU 3.86×，切换纯付码率账），给了 3 个重开条件。
  **→ 待用户拍板。**

---

## 4. 回归纪律（每批提交前）

1. **V7 位流冻结**：全部 golden SHA 不变是硬门禁。
2. V8 改动**只做加法**；必须动共享原语时，V7 位流位精确测试就是安全网。
3. 三命令门禁全绿：ctest 81+、pytest native+shared（670+，随批递增）。
4. 每批独立提交（`feat(v8): 批 N——…` 中文摘要含数据与判定）+ 落档
   `docs/codec/` + 本表批次行更新状态。

## 5. 风险登记

| 风险 | 概率 | 缓解 |
|---|---|---|
| 共享表统计损失 >5% | 低 | 批 0 先测；瓦片粒度/段 ctx 模型备选 |
| 8K 所需 ~6.7 万并发链逼近 81920 lane，高占用下每 lane 速率下降 | 中 | 批 4 校准实测；退一步 8K 用 35ms 档（仍 3×） |
| 段目录/簿记在瘦码流素材上超 2% | 低 | 目录项压到 9B；段粒度 16→32 可信号化切换 |
| CPU 回退路径因段数暴增变慢 | 中 | 批 3 验收门槛（≤5%）盯死；表 LUT 单次构建 |
| 上载墙在 8K 高码流素材显性化 | 中 | ring buffer 摊销 + CRC 重叠（批 5）；统一内存机器免疫 |
| V7 共享原语被顺手改坏 | 低 | golden SHA 硬门禁 |

## 6. 成功标准（立项时冻结，结项对照）

- 码率：V8 vs V7-R2 同 qp **+2~5%**（瘦码流素材封顶 5%）。
  **批 0 实测校准**：qp≤30 实测 +1.4~2.7%（余量充足）；qp44 极瘦源
  （8K 上采样 prores422HQ）单点 +5.2~5.8% 略超——修正路径（接受 ≤6%
  或瘦档 9B 目录）见 `topos_v8_table_loss_2026-09-12.md` §2.3，批 5
  生产切换评审拍板。
- 解码：GPU 全链路 4K ≤ 20ms / 8K ≤ 40ms（6900 XT）；CPU 回退不劣于
  V7-R2 超过 5%；弱 GPU 自动回退。
- 正确性：GPU/CPU/编码 oracle 三方位位精确；段级 conceal == mid 填充。
- 兼容：V1~V7 全部旧流 golden 不变；老二进制拒绝 V8 流为明确错误码。

## 7. 明确不做（本窗口范围外）

- 每段自适应表（禁，§2.1）；帧间编码；新变换/量化；AV1 类复杂上下文
  （段 ctx 模型留二期视批 0 数据）；硬件编解码器对接。
