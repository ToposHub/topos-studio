# V8 批 4：GPU 解码器转正 + 自适应 dispatch（topos_v8_gpu_batch4）

日期：2026-09-12 · 分支：fix-ui · 机器：i9-9900K + AMD RX 6900 XT（PCIe dGPU）
前置：批 1（布局冻结）、批 2（编码器）、批 3（CPU 参考解码器 = 位精确锚）。

## 一、交付物

1. **C 导出** `tc_dev_v8_gpu_export`（codec.c）：scan_v8 + 瓦片表同源解析
   （row_decode）→ 每 segment smeta 14×u32（plane, seg0, nblocks,
   stream_off/len, flags, ctx_n×2, crc_ok, plane_cols, dc0/off0/pairs0,
   tab_index）+ 每 tile 归一化表 709×u16（布局同 tc_dev_packet_slices）。
   支持容量探针（NULL → 仅计数）。
2. **MSL kernel** `rans2_decode_segment`（kernels_dec.py）：每线程一段——
   流 = 4B 终态 + 数据（无前缀）；模型来自瓦片共享表（tab_index）；
   **DC 预测段局部因果**（left=段内前块、top=段内上块行 i≥cols）；
   域检查全保留；conceal = 瓦片 CRC 坏 → 本段 CSR 清零（GPU mid 重建）。
3. **MSL kernel** `inverse_dct_wsd_v8`（kernels_idct.py）：per-block 段映射
   （blkseg）+ per-segment Q 表替换 row_slice 寻址——CSR 原位接续。
4. **差分测试** `tests/shared/gpu/metal/test_metal_v8.py`（5 例）：
   GPU 链（export → 段解码 → wsd_v8）像素 vs CPU 参考解码（ctx 路径 V8
   分派已接入 decoder_ctx.c）**逐值一致 ×4 几何**（444 对齐 / 444 非对齐
   12-bit / 422 色度非对齐 / 422 段跨行 + 高 qp）；conceal 寄存（CRC 字节
   破坏 → GPU status 11 + 与 CPU conceal 像素逐值一致）。
5. **校准基准** `tools/bench_v8_gpu_decode.py` → `tools/v8_gpu_calib.json`：
   4K/8K 真实素材的 GPU/CPU 链路计时 + 择优策略数据源。

## 二、性能实测与 §1.1 外推的修正（本批最重要发现）

| 口径（qp30 真实素材） | GPU rANS | GPU wsd | CPU 全链 | GPU/CPU |
|---|---|---|---|---|
| 4K | 69.8ms | 18.4ms | 22.8ms | 3.86×（CPU 胜） |
| 8K | 850.4ms | 59.2ms | 72.7ms | 12.5×（CPU 胜） |

- **A3 批 1 的线性外推（~3000 链达 CPU 平价、8000 链 4K ~17ms）被实测
  推翻**。链数提升只缩短单链串行延迟（A3 实测的 1.7ns/sym 是
  latency-bound 单链口径），不改变**聚合吞吐**：段化 kernel 在 24K/97K
  线程满占用下聚合 ≈ **1.7ns/sym**（40M sym → 69.8ms），而 16 线程 CPU
  ≈ 0.57ns/sym。二分查表（cum 行 5 级依赖分支、warp 内发散串行化）+
  每符号重整化访存是聚合瓶颈。
- 8K 的 12.5× 额外劣化 = 工作集超缓存（流 130MB + CSR）后 L2 吞吐下降。
- **结论：当前 kernel 设计下 GPU 解码在本机（dGPU）不胜 CPU——自动择优
  正确回退 CPU 路径**（校准策略 `gpu_if_faster` 落入 calib.json）。
  §6 成功标准中"GPU 4K ≤20ms"在当前设计下不可达，登记为设计修正项。
- **后续优化路径**（登记，下一窗口）：①每线程 2~4 链交错（ILP，摊平串行
  依赖延迟——预期 2~4×）；②cum/freq 行 threadgroup LDS 驻留 + 短模型
  LUT 化（消二分发散）；③统一内存机器（Apple Silicon）口径重测——免
  PCIe 上载后链路结构不同。CPU 回退路径性能已达标（批 3），产品无回退
  风险。

## 三、验收（计划 §3 批 4）

| 验收项 | 状态 |
|---|---|
| GPU 全链路 == CPU 参考逐值（4 几何 + conceal + 坏段） | ✓ 5/5（pytest；conceal = CRC 破坏 → status 11 + 像素逐值一致） |
| 4K/8K 基准 vs CPU 落档 | ✓ §二 + calib.json（诚实数字；见外推修正） |
| 校准微基准 + 自动择优回退 | ✓ bench 落档 + `gpu_if_faster` 策略（本机择 CPU） |
| 目标复现 §1.1 外推 | ✗ **外推被实测推翻**（修正记录于 §二；属预期修正而非回归——正确性全过、择优机制按设计工作） |
| 全量门禁 | ✓ ctest 82/82；pytest 697 passed / 9 skipped |

## 四、批 5 预留

- conformance 全套（正流 + fuzz + 损伤矩阵）进 golden 体系；
- `topos_inspect`/CLI 的 V8 补全（已识别，补校验项）；
- 编辑器集成评估（统一内存机器口径 + 上载 ring buffer）；
- 生产切换建议（含 §二 的 GPU 时序结论：CPU 路径为当前生产默认）。
