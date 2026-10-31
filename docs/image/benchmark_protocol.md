# Topos Image 基准测试协议 v0

状态：阶段 0 冻结。所有性能/体积/内存结论**必须**出自本协议；禁止引用未测量数字。

## 1. 环境记录（每次报告必填）

- 机器型号、CPU、核数、内存、存储型号与接口（NVMe/SATA/NAS）；
- OS 版本、编译器与优化 flag（Debug/Release）、库 git commit；
- 线程数（`TOPOS_SLICE_THREADS`）、是否接通电源（笔记本）；
- 文件系统与是否冷/热 page cache。

## 2. 素材集

| 类别 | 内容 | 用途 |
| --- | --- | --- |
| 合成 | 1×1、奇数宽高、非 8 倍数、渐变、极值（全黑/全白/中性灰）、噪声、细线、色卡 | 正确性与最坏码率 |
| 真实 | CG beauty RGB(A)、照片级 RGB、肤色、颗粒 | 质量基线 |
| Alpha | 硬边、羽化、毛发/烟雾、随机噪声遮罩 | Alpha 预算与误差 |
| 序列 | ≥1000 帧合成序列（含空洞/缺帧变体） | 阶段 6 |

真实素材不入库大文件，入库 SHA-256 + 生成脚本；合成素材由 `tests/support/image_synth.c` 扩展生成，保证可复现。

## 3. 分项计时（冻结）

每次测量拆分并分别报告：`open → preamble/directory 解析 → payload 读取 → TPIC decode → 色彩转换 → GPU upload → first present`。禁止只报端到端总数。

## 4. 统计量

每项报告 p50 / p95 / p99 / max，附 CPU%、峰值 RSS、文件句柄数、page faults、磁盘读带宽（`average_file_bytes × fps` 反推校验）。单点均值无意义，不接受。

## 5. 运行矩阵

- Profile：Preview（YUV422 10）、HQ（GBR 10）、XQ（GBR 12）；
- 尺寸：1080p / 2K / 4K / 接近 8K（≤8192）；
- Alpha：无 / A16 mode1 / mode2（记录实际比例与最大误差）；
- 线程：1 / 4 / 物理核数；
- 缓存：冷 / 热；
- 存储：本地 NVMe（基线）、NAS（阶段 6 起必测）。

## 6. 质量指标（与性能同报告）

PSNR、SSIM、每通道最大绝对误差、均值偏移；多代（≥5 代）编解码累计误差；RGB→YUV→RGB 仅对 Preview Profile 单独标注（有 chroma 子采样损失）。与 PNG/TIFF/DPX/EXR 对比时必须同报告：是否无损、位深、Alpha 模式、颜色空间、文件大小——禁止只比体积。

## 7. 历史参照（仅参照，非承诺）

codec 阶段历史基线：i9-9900K、4 线程、4K 10-bit 无 Alpha decode p50 ≈ 109.9 ms；4K+Alpha mode2 ≈ 170.7 ms（`docs/codec/perf_report_stage9.md`）。该数字不可用于图片层宣传；图片层完成时须在本协议下于当前开发机重测。

## 8. 禁止事项

- 引用"350 fps / 2.8 ms"、"5–10× PNG"、"零卡顿"作为完成承诺；
- 用 header parse 速度冒充端到端吞吐；
- 用 mmap 假设零拷贝收益（须实测 vs pread/buffered I/O，含 NAS 尾延迟）；
- 报告缺环境记录或缺分项计时。
