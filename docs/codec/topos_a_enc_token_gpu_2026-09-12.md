# A-enc（速度计划 A 轴编码 GPU 化）批 1——token 差分机 + quant_token kernel + PCIe 判定（2026-09-12）

**背景**：A2 稀疏契约反转用于编码——CPU 后向 rANS 环真正需要的不是
稠密 F（99.5MB/帧，A1 已判 PCIe 净亏）而是稀疏 token（run, lvl 对 +
DC + npair）。若 GPU 完成 DCT→量化→扫描→token 化，CPU 只剩不可并行
的后向环（C5 复剖析：占 r2e 75.5% 的串行依赖地板）。

**结论**：

1. **差分机与 kernel 资产落地（位精确）**：C token 捕获 API
   （`tc_dev_set_tok_capture` 族：dc_m/npair/run/lvl_m + 每 band
   Q/half/dz/**fastdiv 孪生**表——单一真相源）+ `quant_token` MSL
   kernel（组=块布局：64 lane 并行量化 + threadgroup 暂存 + lane 0
   zigzag 扫描发射；除法以 AVX2 内核同式 magic 精确复刻）。
   三方差分（C == numpy == GPU）4/4 配置全过（444/422 × 10/12bit ×
   qp 30/45/58/63 × 对齐/非对齐）。**语义细节钉死：DC 预测上下文是
   带局部的**（fill 每带 memset——带首行无 top，slice 独立性）。
2. **本机生产集成判负（PCIe token 下行墙）**：4K qp58 token = 7.12M
   对 = 57MB（u64）/ 28.5MB（u32 紧包）——**≥5.2ms 单向传输地板**
   （信息量下界，不可压缩：rANS 输出本身就是其压缩形）。GPU kernel
   写 Shared pairs 缓冲实测 11.3ms/平面 ≈ 62MB÷5.5GB/s，与 A1 实测
   PCIe 带宽精确一致。被替代的 CPU fill 仅值 ~2.7ms 墙钟（~30ms
   线程时 / ~11 线程）→ **codec 轴（CLI/产品编码，帧在 CPU）净亏
   ~3ms/帧**。
3. **编辑器语境与统一内存机器为正收益条件**（同 A1 判式）：帧驻 GPU
   时省去平面下行（47MB）+ fill，仅付 token 下行（28.5MB）≈ +6ms
   级收益——属编辑器集成批（产品化工作）与统一内存机器（Apple
   Silicon，无 PCIe 墙）触发条件。

## 一、测量（4K 444 10bit qp58 真实素材帧）

| 项 | 值 |
|---|---|
| token 规模 | 102 带 / 388800 块 / **7.12M 对**（最坏 24.5M 预留的 29%） |
| noop dispatch 地板 | 0.168 ms |
| quant_token 单 dispatch（整平面，Shared pairs 出） | 11.3 ms |
| 34 带真实形态（含 dispatch 链） | 22.9 ms |
| CPU fill（被替代项，线程时/墙钟） | ~30 ms / ~2.7 ms |
| token 下行地板（u32 紧包 28.5MB @ 5.5GB/s） | ≥5.2 ms |

kernel 本体计算近免费（magic fastdiv + 组=块布局后瓶颈全部在
Shared pairs 的 PCIe 写）——散装载入首版 26.5ms/平面（63 次跨 256B
4B 载入/块）为反例教训，重构布局正确但治不了 PCIe。

## 二、交付

- `native/topos_codec`：`tc_dev_set_tok_capture` 捕获族 +
  `tc_dev_zigzag` 访问器（codec.h/codec.c；frame_encode_ready 主线程
  惰性扩容，worker 段基几何推导无竞争，run/lvl 最坏段 + 压实 getter）；
- `src/shared/gpu/metal/kernels_enc.py`：`quant_token`（组=块，
  fastdiv magic）；
- `tests/shared/gpu/metal/test_metal_kernels_enc.py`：三方差分 4/4。

## 三、门禁

ctest 81/81；pytest tests/native+tests/shared 661 通过（新增 enc 差分
4 例）。

## 四、证据

- /tmp/a2_prof/enc_b1_bench.py（token 规模 + kernel 墙钟）、
  enc_b1_split.py（dispatch/计算分离 + noop 地板）；
- C5 剖析（`topos_c5_r2e_stage_profile_2026-09-12.md`）提供后向环
  75.5% 地板依据。

## 五、判定与移交

- A-enc 生产集成 = **条件项**（统一内存机器或编辑器 GPU 驻留语境），
  与 A1 同列；kernel/差分机资产保留复用；
- 本机编码侧剩余杠杆：C5 已收口（+6.4% 已入库）；下一步数量级只在
  B 轴（AVX-512 换机）或 A3（V8 格式，GPU 侧 rANS 解锁后 token 不再
  下行——终局形态）。
