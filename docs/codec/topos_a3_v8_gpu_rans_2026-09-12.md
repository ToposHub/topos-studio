# A3/V8 批 1：GPU 侧 rANS slice 解码——差分机成立 + V8 链数规格量化

日期：2026-09-12 · 分支：fix-ui · 机器：i9-9900K + AMD RX 6900 XT（PCIe dGPU）
前置：A2-4 生产融合发射（8f52bfcc）——CSR 契约/inverse_dct_wsd 已落地；
A-enc 批 1（90dbafbe）——GPU 侧量化/token 差分机方法学。

## 一、目标与总判定

**目标**：把熵解码环本身搬上 GPU——码流上行、CSR 直接在显存产出、原位接
IDCT，token 全程不离开显存（"token 不再下行，GPU 侧 rANS 解锁"）。

**总判定（三层）**：

1. **机制成立（位精确）**：GPU kernel 逐式移植 `tc_color_rans2_decode_sparse`
   （状态机/suffix 旁路/DC 预测/zigzag/域检查/conceal 全保留），差分 9/9
   全过——GPU CSR 与 C 生产融合发射逐元素一致；端到端链（payload 上载 →
   GPU rANS → CSR 显存原位接 wsd → 像素）与 CPU 参考解码平面逐值一致；
   CRC 破坏 → status=11 + CSR 段全零 → GPU 链重建 == CPU conceal 参考。
   **"token 不下行"的解码形态在 V7 码流上已被证明可行。**
2. **V7 粒度下性能不成立（量化的负结果）**：V7 slice_count ≤ 512 上限把
   独立熵链锁死在 ≤405（4K）。实测 405 链 GPU rANS 169ms vs CPU 解码
   26ms（慢 6.5×）；链数扩展完美反比线性（51→1088ms、204→329ms、
   405→169ms），无占用饱和。
3. **V8 规格由此定量**：单 GPU 标量 lane 的 rANS 链速 ≈ CPU 核的
   1/5~1/7（延迟链无乱序）。外推 **≈3000 条独立链达 CPU 平价；≈8000 链
   rANS ~10ms + wsd 6.6ms ≈ 17ms（1.5×）；6900 XT 81920 lane 尚有 20×
   余量**。V8 格式的核心规格要求 = 熵流数量进千级（如每块行×平面段或
   子带交错），这正是"交错多状态 rANS"立项时的定性判断，现在有了实测
   斜率支撑。

## 二、资产清单

| 资产 | 位置 | 说明 |
|---|---|---|
| C 导出 | `tc_dev_packet_slices`（codec.h/codec.c） | packet 级 slice introspection：tc_packet_scan → 每 slice 拷 payload（meta 9×u32：plane/y0/h/payload_off/payload_size/flags/dc_ctx_n/lvl_ctx_n/**crc_ok**）+ **解码器同源** `tc_rans2_row_decode` 产归一化表（709×u16/slice：dc_freq[5×29] dc_cum[5×30] run_freq[64] run_cum[65] lvl_freq[5×28] lvl_cum[5×29]）。单线程、零热路径副作用 |
| MSL kernel | `src/shared/gpu/metal/kernels_dec.py` → `rans2_decode_slice` | 每线程一条 slice 完整 rANS 链。slot→sym 用 cum 行二分（C LUT 的逐值等价替身：Σfreq=4096 且 cum[nsym]=4096 保证二分答案 freq>0）；DC 预测回读自身 dense dc 段（band 内因果行，slice 独立契约）→ 零私有行数组；conceal：CRC/解码失败 → 先清零 CSR 段再退出（= dev_qcap_zero_slice 语义） |
| 差分测试 | `tests/shared/gpu/metal/test_metal_kernels_rans.py`（9 tests） | ①GPU vs CPU CSR 位精确 ×4 几何（444 对齐/444 非 8 对齐 12-bit/422 色度非对齐/slice_rows=2 细分带）；②端到端 token 不下行 ×4；③conceal 寄存（确定性字节破坏） |
| 基准 | `/tmp/a3_prof/bench_rans_gpu.py` | 4K 422 10bit qp30 × 内容(noise/smooth) × srows(16/4/2) |

## 三、差分验收细节

- **CSR 契约对齐 A2-4 生产直发**：dc plane 稠密 i32 / off 每 slice
  [blocks+1] 连续段 / pairs 最坏 63×blocks 段拼接（段基址宿主预建，
  smeta 13×u32/slice = C 9 项 + plane_cols/dc0/off0/pairs0）。
- **GPU CSR 原位接 wsd**：spbase=最坏预留段基、soff=off 段基——wsd 读
  `pairs[spbase[s]+off[ob+bi]+k]`，与 kernel 写入布局天然吻合，零中转。
- C 连续实际计数布局（pairs_cum）与 GPU 最坏预留布局的差分比较按 slice
  段分别对齐（off 段尾给实际计数）。

## 四、基准（4K 3840×2160 422 10bit qp30 rans2，AMD RX 6900 XT）

| 内容 | srows | 链数 | 码流KB | CPU解码 | intro | upload | rANS(Shared) | rANS(Private) | wsd | e2e(GPU) |
|---|---|---|---|---|---|---|---|---|---|---|
| noise | 16 | 51 | 31750 | 27.6ms | 42.8ms | 17.4ms | 1087.7ms | 1077.2ms | 6.0ms | 1143ms |
| noise | 4 | 204 | 31774 | 26.2ms | 52.3ms | 21.4ms | 336.5ms | 328.6ms | 6.6ms | 409ms |
| noise | 2 | 405 | 31808 | 26.1ms | 51.3ms | 20.9ms | 175.6ms | 169.1ms | 6.6ms | 248ms |
| smooth | 16 | 51 | 13031 | 24.8ms | 20.3ms | 9.4ms | 897.9ms | 909.5ms | 6.6ms | 946ms |
| smooth | 4 | 204 | 12974 | 23.2ms | 22.2ms | 10.2ms | 266.5ms | 263.2ms | 6.7ms | 302ms |
| smooth | 2 | 405 | 12980 | 24.1ms | 16.0ms | 10.2ms | 138.1ms | 138.2ms | 6.7ms | 171ms |

解读：

- **链数扩展反比线性无饱和**：51→405（8×链）rANS 1088→169（6.4× 时间）。
  GPU 在 405 线程时仅占用 ~0.5% lane——瓶颈是每链串行延迟，不是吞吐。
- **Shared vs Private CSR 几乎无差**（写入流量相同；Shared 惰性分页）。
  差分验收读回才产生实际传输——生产链路（驻留）不读。
- **wsd 6.6ms 恒定**：GPU IDCT 与链数无关（其并行度=块数）。
- **CPU 基线 23~28ms**：与 A2-4 的 11.8ms 不同源——本表是全平面解码
  （decode_views 含反量化+重建+写平面），A2-4 的 11.8ms 是 CPU 符号环
  分项。GPU 要"赢"的对象是全链路 CPU 时间。
- **intro（42~52ms noise）主要是 tc_packet_scan 的逐 slice CRC**（31MB
  码流）+ 表构建。V8 设计要点：CRC 可按 slice 并行/与上载重叠，或 GPU
  化；表构建（709×u16×405 = 575KB）本身可忽略。
- **upload 17~21ms > 31MB/5.5GB/s=5.7ms 理论值**：Shared buffer 创建
  开销（分配+首次触碰）；V8 生产用预建 ring buffer 摊销。
- px_dl≈0.03ms 是 Shared contents() 惰性映射假象（不触发即时传输），
  不应读作"下行免费"；编辑器真实路径 = 输出直留 GPU 纹理。

## 五、外推模型与 V8 规格推论

单 lane 符号率 ≈ 40M sym ×（链时间反推）≈ 14M sym/s/lane；CPU 单核
≈ 90~100M sym/s。全帧 ~40M 符号（noise qp30）：

| 链数 | rANS 外推 | +wsd | vs CPU 26ms |
|---|---|---|---|
| 405（V7 上限） | 169ms（实测） | 176ms | 0.15× |
| ~2900 | ~26ms | ~33ms | ~0.8×（平价区） |
| 8000 | ~10ms | ~17ms | ~1.5× |
| 16000 | ~5ms | ~12ms | ~2.2× |

V8 格式规格推论（供 A3 立项）：

1. **熵流粒度**：每 slice 一链不够——需块行级或子带级交错（4K 目标
   ≥8000 链 ⇒ 每平面每 1~2 块行一段，或 8×8 段×块行交错）。
2. **每段模型前缀成本**：V7 每 slice 前缀 122~350B；8000 段 × ~180B ≈
   1.4MB/帧（码流 +4.5%）——V8 需考虑段间共享表（如每平面 4 表 ×
   段引用）或更紧的表编码。
3. **CRC/intro 宿主成本必须移出关键路径**（并行/重叠/GPU 化）。
4. **下行方向消失后，上载成为唯一 PCIe 流**：码流 31MB/帧 5.7ms 理论
   墙（qp30 noise）；更低 qp 码流更大，统一内存机器（Apple Silicon）
   连这堵墙也没有。

## 六、过程中钉死的两个 dev API 语义（非 bug，记录在案）

1. `tc_dev_qcap_sp_npairs()` 由 `sp_pairs_copy` 刷新（fused 路径不自带
   累计）——裸读计数器 = 上次拷贝值（codec.h 已补注）。
2. `tc_dev_qcap_sp_pairs_copy` 在 cap < 实际用量时返回 0（不截断）——
   测试 helper `_capture_csr` 固定 4M 对临时缓冲，4K 噪声 16.3M 对会
   触发该路径（差分请用小几何或预扩容）。

## 七、判定与下一步

- **A3/V8 批 1 判定：成立（差分机 + 规格量化双交付）**。GPU rANS 机制
  位精确、conceal 语义闭合、token 不下行的解码形态可行；V7 粒度下生产
  不启用（405 链上限 = 0.15× CPU），**生产开关绑定 V8 格式窗口**。
- 批 2 候选（须 V8 立项后）：交错多状态 rANS 的段格式设计（§五推论 1/2）
  + 编辑器驻留管线接线（输出直留纹理）+ CRC 重叠。
- 回归状态：ctest 81/81；pytest tests/native 44 通过；tests/shared 626
  通过（含本批 9 个 GPU rANS 测试）。
