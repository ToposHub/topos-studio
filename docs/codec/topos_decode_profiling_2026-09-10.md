# Topos 解码热路径剖析与优化战役判定（2026-09-10）

**背景：** 优化战役第 3/4 步（SIMD IDCT、切片负载均衡）的执行前勘察。
**机器：** i9-9900K（8C16T）　**探针：** `sample` 采样 + 阶段统计（勘误
口径）+ 切片布局对照实验。**素材：** 8K HQ V2 VLC + flat（552 Mbps 产品
目标，qp68）与 8K HQ V1 Rice（2.1 Gbps，qp56）。

## 结论（如实）

1. **"写 SIMD IDCT" 这一步已经存在于代码库**——解码栈已含 AVX2 融合
   反量化+逆变换内核（行稀疏跳转 + 内联反量化 + hi/lo 精确分解 +
   对称输出）、SSE2 整行像素存储（M4）、DC-only 闭式常数填充（M4，
   真实素材命中率 ~50%）、稀疏 basis 直存（M10-2B）。此前"IDCT 占
   71%/98.6%"的阶段统计口径包含整条 scan→sink 流水线，不能按字面
   理解为"变换缺 SIMD"。
2. **生产口径采样（8K V2，1t）**：AVX2 融合内核 45%、扫描循环
   （VLC 符号解码+散射）37%、存储 15%、CRC 1%。融合内核对小 nnz 块
   （平均 3.7 个非零 AC、非零频率行 ~2-3）有约 4× 于算术量的固定
   脚手架开销——理论重写上限 ~15% 整体解码收益，风险高（bit-exact
   内核），ROI 不足。
3. **切片负载均衡实验（负结果）**：slice_rows 4/8/16/32（51~408 切片）
   16t 解码 49.5~50.7 fps（差 <2%）。16t/1t 加速 7.3× 的天花板是
   16 线程写 ~100 MB/帧平面的**内存带宽争用**（16t 下融合内核累计
   耗时较 1t 膨胀 1.9×），不是切片调度问题。第 4 步关闭。
4. **产品实时性盘点（优化战役后终态）**：全部产品配置 ≥2× 实时——
   8K HQ V2+flat 50.5 fps（16t）；4K 全档 77~278 fps；2K 全档
   229~682 fps。与 ProRes 的残余解码差距（2K 0.39~0.92×、4K
   0.62~1.48×）是算法密度差（ProRes 变换动态范围小、舍入简单），
   属内核重写级别的长期议题，当前无产品痛点。

## 剩余可做（按 ROI 排序，未执行）

- 扫描循环 37%：VLC LUT 局部性/批解码微优化（~10% 整体上限）；
- 融合内核小 nnz 特化（≤15% 上限，bit-exact 验证成本高）；
- 16t 带宽：平面写非暂时性存储（NT store）试验——带宽受限场景或
   有 5~10%，需实测。

## 复现

```bash
# 探针（本仓 src/simd/transform_avx2.c、src/codec/color_store.h 已有全部分析对象）
cc -O2 -I native/topos_codec/include -I native/topos_codec/src decprof.c \
   -o decprof native/topos_codec/build/perf/topos_codec.dylib
sample <pid> 5                      # 生产口径热点（勿开 TOPOS_CODEC_PROFILE，
                                    # 统计路径会加每块 64 次非零扫描）
TOPOS_SLICE_THREADS={1|16} ./decprof file.mov 3   # 阶段统计需 TOPOS_CODEC_PROFILE=1
# 切片布局对照：--slice-rows {4,8,16,32} 编码后测解码（差 <2%）
```
