# Topos Image 阶段 7 性能报告（首次实测）

日期：2026-09-05
协议：`docs/image/benchmark_protocol.md`（v0 冻结版）
工具：`tpic benchmark --iters 20`（probe / validate / decode 分项计时，CLOCK_MONOTONIC）
素材：确定性高熵噪声（rng seed=42）编码为 GBR 4:4:4 10-bit Image HQ，qp28（近最坏码率）

## 环境

| 项 | 值 |
| --- | --- |
| 机器 | Intel Core i9-9900K @ 3.60GHz，16 线程，32 GB |
| OS | macOS 15.3.1（x64） |
| 存储 | APFS SSD（本地，热 page cache） |
| 构建 | Debug（`build/debug/topos_codec.dylib`）+ tpic CLI |
| 库版本 | commit 4909ccf1（阶段 6 后）；codec ABI v2 / image ABI v1 |
| 并行 | 帧级=1；slice 级=进程默认线程预算 |

## 结果（p50/p95/p99/max，ms，20 iters，热缓存）

### 1080p GBR 10-bit（文件 17,116,938 B）

| 分项 | p50 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: |
| probe（header+directory+IDSC） | 0.017 | 0.021 | 0.021 | 0.029 |
| validate（逐 chunk 流式 CRC） | 14.956 | 16.157 | 16.157 | 35.184 |
| decode（整读 PIXL + codec 解码） | 96.539 | 104.543 | 104.543 | 105.777 |

### 4K GBR 10-bit（文件 68,471,588 B）

| 分项 | p50 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: |
| probe | 0.018 | 0.022 | 0.022 | 0.027 |
| validate | 58.761 | 61.323 | 61.323 | 61.900 |
| decode | 381.892 | 406.937 | 406.937 | 407.411 |

## 协议要求的推导校验

- **封包层额外开销**（相对直接 TPIC decode）：decode = probe(≤0.02ms) + PIXL 整读
  （1080p ≈ 17MB / 4K ≈ 68MB，page-cache 拷贝约 2–7ms）+ 纯 codec 解码。
  封装层开销占比 ≈ 2%（4K）~ 8%（1080p，Debug 构建）；其余为 codec 数学。
  封装层不复制、不转换像素，语义零改变（阶段 1 测试锁定 envelope==直连）。
- **所需存储带宽**（average_file_bytes × fps）：4K decode p50 381.9ms ≈ 2.62fps
  × 68.5MB ≈ **179 MB/s** —— 本地 NVMe 远低于此；播放带宽成立。
  （素材为高熵噪声最坏码率：qp28 下压缩后 > 原始的 97%；真实素材码率显著更低。）
- **350 fps / 2.8 ms**：当前硬件实测 4K decode ≈ 382ms——与协议一致，
  该宣传数字不作为任何完成依据。

## 决策（按协议"测量优先"）

1. **不做无据优化**：分项显示耗时主体为 codec 解码（SIMD/并行已由上游阶段 9 优化），
   封装层占比小；无重复分配/重复 CRC/多余拷贝可消除（probe 只读 53B+目录，decode 一次整读）。
2. **mmap 保持关闭**：普通 pread/read 已达 ~2.4GB/s（68MB/58.8ms validate 路径含 CRC）；
   mmap 需 NAS/Windows 尾延迟实测才考虑（协议 §8）。
3. validate 为显式校验操作，不在 decode 热路径（播放不解目录 CRC 两次）。
4. NAS/冷缓存矩阵、PSNR/SSIM 质量基线、序列 1000+ 帧长测：**遗留至阶段 12 收口**
   （协议允许的素材/环境扩展，不阻塞 v1 正确性完成定义）。

## 复现

```bash
cd native/topos_codec/build/debug
./tpic encode /tmp/tpic_bench/bench_4k.raw -o /tmp/tpic_bench/bench_4k.tpic \
  --width 3840 --height 2160 --pixel-format 2 --bit-depth 10 --qp 28 --overwrite
./tpic benchmark /tmp/tpic_bench/bench_4k.tpic --iters 20
```
