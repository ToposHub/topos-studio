# A2 解码 GPU 化一期（速度计划 v2）——批 1/2 落档

日期：2026-09-12　分支：fix-ui　提交：6db63400（批 1）+ 本批
机器：i9-9900K + Radeon RX 6900 XT（PCIe dGPU）

## 一、交付物

### 1. native 解码侧量化系数捕获（dev 差分机）

`tc_dev_set_qcap_capture(1)` 后，decode_frames 批内 V1/V2/V7-R/R2 颜色
slice 改走 `tc_color_slice_decode`（q_out 版，与生产直写路径同 core 同
语义），把重建量化系数（natural 序稠密 `[block_h][cols][64]`，DC 预测
已折叠）连同每 slice 反量化步长表 `Q[64]` 拷到 dev 缓冲：

- `tc_dev_qcap_copy` / `tc_dev_qcap_meta_copy`（`tc_dev_qcap_slice`：
  plane/block_y0/block_h/cols/qp_eff/off_elems）/ `tc_dev_qcap_q_copy`；
- 捕获模式下输出平面不写——参考平面用关闭捕获的常规解码另取（确定性
  解码保证两次 q 一致）；
- V3/V6 intra（语义不同）与 V7-A band（不经 dec_slice_job）不在范围。

**实现教训（已修）**：捕获区域指针必须在整个批内稳定——逐 slice
`tc_realloc` 会让先前任务已分配的区域悬空（use-after-free 堆损坏，
首发实测：rans2 读态变垃圾 → slice conceal、进程 abort）。现为
`dec_frames_core_impl` 批入口按全部 slice 精确总量一次扩容
（`dev_qcap_prepare_batch`），`dec_slice_job` 的 reserve 只做区域切分。

### 2. 稀疏系数契约（CPU rANS 符号环 → GPU）

- `dc[nblocks]` int32（DC 预测已折叠，与 sink 消费的 q 同源）；
- `pair_off[nblocks+1]` uint32（平面级 CSR，块行主序）；
- `pairs[npairs]` uint64：`(uint64)(int64)level << 6 | nat`（nat=natural
  系数号 1..63；MSL `(long)pk >> 6` 算术右移恢复符号）；
- `qtables[nslices*64]` uint32 + `row_slice[rows]` uint32（slice = 块行带）。

### 3. MSL kernel（`kernels_idct.py`，四个位精确变体）

数值契约（与 color_store.h/transform.c 规范 scalar 逐位一致）：
`F' = clamp(q·Q, ±2^25)` → `G = F'⊙W`（|G|<2^39）→
`x̂ = round(MᵀGM / 2^32)`（单一最终舍入、符号拆分四舍五入；整数乘加
int64 域 |acc|<2^53 可任意重排 = GPU 结构自由）→ `clamp(x̂+mid)`。

- `inverse_dct_px`：一线程一像素 64 项直和（差分机契约基准）；
- `inverse_dct_m8`：一组 8 块两遍（G→B→像素，int64 MAC 每块 4096→1024）；
- `inverse_dct_sp`：m8 同构 + CSR 对散射；
- `inverse_dct_w1`：**一块一组（64 线程 = RDNA2 单 wave）——生产选型**；
- 行掩码（m8/sp/w1 共用）：B 相扫描 G 的 u 行占用（x==0 线程
  `atomic_fetch_or` 置位，清零/置位分居 barrier 两侧无竞态），空行
  B=0 且像素相跳读——G 全零 ⟹ 各项恒 0，位不变。4K 稳态 u 行占用
  ~3.05/8 → MAC 降 ~2.6×。

## 二、验收

三方差分（native 解码平面 == numpy 整数参考 == Metal）逐字节一致：

- 真实包 4 例（encode→decode 全路径捕获）：rans2 × {96×64 10bit 对齐、
  313×141 12bit 非 8 对齐、150×90 422（色度 75 宽非对齐）} + V1 Rice；
- 合成 q 2 例（DC-only 块、±2^25 防御钳位边界、多 slice）；
- 4K 444 qp58 稳态包（steady.tpc）：三个平面 Shared 与 Private(VRAM)
  双模式 diff=0。
- 测试：`tests/shared/gpu/metal/test_metal_kernels_idct.py` 6 例；
  Metal 套件 100/100、native C 81/81、binding/ABI 45 过。

## 三、4K 444 qp58 实测（VRAM/Private，dispatch_sync p50×30）

| 项 | 数值 |
|---|---|
| 空 kernel 同网格地板 | 0.163 ms/平面 |
| **w1 kernel** | **0.24~0.28 ms/平面（整帧 ~0.8 ms）** |
| m8（稠密） | 0.29~0.38 ms/平面 |
| sp（512 线程组） | 0.59 ms/平面 |
| px（稠密直和） | 3.7 ms/平面 |
| 稀疏上载 | 2.5~2.65 ms/平面（~12 MB：pairs 8B×1.38M 为主） |
| native 对照 | 解码 16t 16.4 ms/帧；sink（反量化+IDCT+写）CPU 113 ms/帧、wall 份额 67% |

方法论坑（已修，留档防重蹈）：PyObjC `newBufferWithLength:options:`
的 `MTLResourceStorageModePrivate` ≠ 2（2 是 cache 位；正确常量经
`ctx._metal.MTLResourceStorageModePrivate` 取，=8）——首测误用 Shared
导致所有"Private"数据实为系统内存，kernel 墙钟 3.4 ms/平面全是输出
写穿 PCIe（空 kernel 2.95 ms 地板 expose 了这一点）。

## 四、判断

1. **A2 形态成立**：编辑器语境（输出=GPU 纹理零回读、稀疏上载与 CPU
   符号环重叠）下，解码墙钟投影 ≈ CPU 熵环（55.6 ms CPU/帧 ≈ 6 ms
   wall@16t）+ GPU 0.8 ms ≈ **7 ms vs native 16.4 ms ≈ 2.4×**，好于
   立项估的 1.3~1.8×。GPU 侧计算已近免费（空 kernel 地板之上 ~0.1 ms），
   剩余成本全在上载（可与 CPU 重叠）。
2. CLI 语境（需回读 3×16.5MB ≈ 7.8 ms）≈ 14.6 ms vs 16.4 ms，仅
   ~1.1×——与立项预判一致，编辑器路径是主战场。
3. 后续（A2-3，编辑器集成批）：C 侧 scan 直接产 CSR 对（现差分机为
   稠密捕获 + Python 稀疏化，生产须免中转）；纹理输出（R16Uint）；
   编辑器路径 A/B wall 验收。可选微优化：pairs u32 打包
   （26bit level+6bit nat，AC 域 |level|≤2^25 但 +2^25 恰溢出 26bit
   符号域——需钳位证明或 escape，暂不做）。

## 五、证据

- 基准脚本：/tmp/a2_prof/{idct_bench.py, idct_private.py, floor_test.py}
  （steady.tpc = 4K 444 qp58 产品稳态包）；
- 复剖析：sink 67%（entropy 55.6 + sink 113.3 ms CPU/帧，TOPOS_CODEC_PROFILE）；
- 位精确：见 §二。


## 六、批 3（编辑器集成批，同日追加）：C 直产 CSR + 端到位精确 + A/B 初测

### 交付

- native：`tc_dev_set_qcap_sparse(1)`（与 qcap 捕获联用）——worker 解码
  slice 后扫描稠密 q 直接发射 CSR：dc（plane 稠密）+ 每 slice off 段
  （**块前累计**语义，首版块后累计差一位已修）+ pairs 段（最坏 63 对/块
  预留，getter 压实拷贝；实际对数 = off 段尾）。段基址全由 meta 确定性
  推导，免 post-join 组装；sp 三缓冲批入口一次精确扩容（512MB 上界）。
- MSL：`inverse_dct_ws`（w1 同构 + slice 分段 CSR：sblk0/spbase 辅助
  数组 + off 定长分段）。**spbase 必须用压实累计位置**（各 slice 实际
  对数解码后才知——生产集成同此，基址表 post-join 填；首版用最坏段
  基址差 1 对即全平面错，已修）。
- 验收：`test_c_sparse_emission_ws` 3 例（444 对齐/非对齐 12bit/422
  Rice）端到端逐字节一致；4K steady.tpc 全管线（捕获发射→上载→ws
  kernel→回读）三平面 diff=0。

### A/B wall 初测（4K steady.tpc，16t）

| 路径 | 墙钟 |
|---|---|
| A：native 解码 | 14.6 ms |
| B0：捕获+稀疏发射解码（dev 钩子，无 getter/分配） | 22.5 ms |
| GPU：ws kernel（批 2 干净口径，持久缓冲） | 0.25 ms/平面 |
| GPU：稀疏上载（批 2 口径，可与 CPU 重叠） | ~2.5 ms/平面 |

**诚实判读**：B0 比 native 慢——dev 钩子走非融合 q_out 通用路径 +
稠密 q 中转 + 发射扫描，这是差分机不是生产路径。生产融合发射
（rANS 符号环内直出 (nat,level) 对、跳过稠密 q 与 63 扫描——环内
run/level 本已在手，打包 u64 近似免费）为 A2 收尾剩余项；投影 CPU
≈ 熵环 6~7 ms + GPU 重叠 → **~2×**。编辑器 GUI 接线（纹理绑定/
环形缓冲/帧流水）属产品化工作，不在 codec 轴立项表内。

### 证据

- /tmp/a2_prof/ab_wall.py（A/B + 4K 位精确验收 PASS）；
- 测试：test_metal_kernels_idct.py 9/9（新增 C 发射链路 3 例）。

## 七、批 4（A2-4 生产融合发射，同日）：rANS2 环内直出 CSR + 直发 GPU 可见内存

### 交付

- **融合稀疏核** `tc_color_rans2_decode_sparse`（slice_codec.c）：与
  通用核同语义/同错误码/同消费位（差分钉死），符号环内 (nat,level)
  直接打包 u64 发射——无 blk[64] 稠密中转、无 memset、无 sink 调用、
  无 63 扫描。DC 重建值双写（dc 池行 + plane 稠密发射区）。发射序 =
  zigzag 解码序（与 dense-scan 自然序不同；zigzag 双射 + kernel 求和
  序固定 → 位精确等价，测试按块多重集钉死）。
- **生产接线**（dec_slice_job）：`sparse=1` 且 V7-R2 走融合核；
  `sparse=2` 强制 legacy 扫描（差分 oracle）；其余熵族自动 legacy。
- **conceal 语义闭合**：slice 失败（含 CRC 早失败——发生在解码分支
  之前，此前 CSR 残留上一帧/未初始化）统一清零该 slice 的 CSR 段；
  全零系数 GPU 重建 = round(0)+mid = mid，与 conceal_band 的 bd_mid
  填充逐值一致（测试钉死）。
- **直发目标 API** `tc_dev_qcap_sp_set_targets`：稀疏三缓冲改用调用方
  pinned 内存（典型 = MTL Shared），布局不变（pairs 最坏段/off 连续
  段/dc plane 稠密）→ spbase/soff/d c0 全部确定性可预建，**每帧零
  getter/零 join/零上载趟**；超容自动回落内部模式。capture(0) 会话
  重置同步修复稀疏计数无限增长缺陷。
- **kernel** `inverse_dct_wsd`：ws 同构 + 连续 off 段（soff[s] 段基）
  + dc0 平面段基（params[7]）——直接消费 C 原生布局。

### A/B wall（4K 444 qp58 steady，真实素材帧，p50×30，16t）

| 路径 | 墙钟 |
|---|---|
| A：native 解码（熵环+IDCT+平面写） | 18.5 ms |
| B0：legacy 扫描发射（批 3 dev 钩子改 sparse=2） | 16.8 ms |
| B1：融合发射（内部缓冲 + getter/join/上载胶水） | 11.9 ms |
| **B2：融合发射直写 pinned（生产形态）** | **11.76 ms** |
| B2 dispatch：wsd×3（Shared 按需 PCIe 读 ~35MB） | 10.68 ms |
| B2 串行合计 | 22.4 ms = **0.82×**（CLI 读回墙不变） |
| **B2 帧流水重叠（max(CPU,GPU)）** | **~11.8 ms = 1.57×** |

- 融合核省 4.9 ms CPU（vs B0，−29%）；vs native 整体 CPU 18.5→11.8
  （−36%，含免 IDCT/平面写）。**投影修正：~1.6×（原估 ~2×）**——
  剩余 CPU = rANS 符号环自体（C5 靶子），GPU dispatch 为 PCIe 按需
  读 bound（3.3GB/s 有效；Managed-flush/预拷 Private ≈7 ms 仍藏在
  CPU 之下，重叠上限 = CPU）。
- 正确性：4K 全管线 ws/wsd × 内部/直发 3 平面 diff=0；融合 vs
  legacy CSR 逐值（dc/off）+ 块内多重集（pairs）等价；conceal 注错
  （CRC 路径）GPU mid == CPU conceal。

### 门禁

ctest 81/81；pytest tests/native+tests/shared 657 通过；
test_metal_kernels_idct.py 14/14（新增融合/legacy 差分 4 例 + conceal
1 例 + wsd 双 variant）。

### 证据

- /tmp/a2_prof/b1_wall.py（B0/B1 分项 + 4K 校验）、b1_direct.py
  （B2 直发管线 + 4K 校验 diff=0）；
- 剩余：编辑器 GUI 接线（纹理绑定/环形缓冲/帧流水）属产品化工作；
  codec 轴 A2 收口。解码进一步加速移交 C5（符号环）与 A3（V8 窗口）。
