# ADR-I004：阶段 4 —— 完整 C ABI、tpic CLI 与格式互转

日期：2026-09-05
状态：已接受
前置：ADR-I001/002/003

## 决策与交付

| 项 | 内容 |
| --- | --- |
| ABI 补全 | `tc_image_query_capabilities()`（能力掩码：profiles_mask/metadata_mask/tile_roi 等非目标恒 0）与 `tc_image_derive_idsc()`（从合法 packet 派生十项交叉一致的 IDSC；编码器/适配器禁手拼字段）。至此 probe/validate/decode/metadata/encode(write)/capabilities 全部就位 |
| 取消语义 | v1 全部入口为单发无阻塞调用（无长循环），`tc_image_cancel` 不适用；序列阶段的取消由阶段 6 的 generation 模型承载，不在单图 ABI 引入 |
| CLI | `tpic probe|verify [--deep]|decode -o|encode -o|benchmark [--iters]`，退出码 0/1/2 对齐 topos_inspect |
| 原子写 | 同目录临时文件（`*.tpic.tmp-<pid>-<rand>`）→ 写满 → fflush+fsync → 重新 probe/validate 自检 → rename；失败 unlink；默认拒绝覆盖（--overwrite 显式）；绝不静默换格式/扩展名 |
| decode 输出 | planar uint16 LE + `.json` sidecar（几何/位深/通道名 G,B,R|Y,U,V,A/色彩标签/concealed_slices）；**精度与色彩转换零静默** |
| benchmark | 按 benchmark_protocol 分项计时（probe/validate/decode），p50/p95/p99/max |
| 格式互转 | `scripts/tpic_tools.py`（适配层，允许第三方依赖）：.tpic↔PNG 用 cv2（Pillow 不支持 16-bit RGB PNG）；GBR 平面序 G,B,R ⇔ PNG RGB 显式交换；YUV Preview → PNG **显式拒绝**（避免静默猜 sRGB，spec §7）；位深缩放显式报告（8/16-bit 源 → 10/12-bit 的舍入位数） |
| 核心零依赖 | libtopos_codec 不引入任何第三方图像库；PNG 依赖只在适配层脚本 |

## 验收门槛核对

- 不带第三方图像库也能构建核心 ✅（CMake 无新依赖）
- CLI 任何精度/色彩转换显式显示 ✅（decode sidecar + 适配器打印）
- PNG 往返按其可表达能力验收 ✅（16-bit PNG 容器；8-bit 源声明舍入位数）
- 失败不生成伪成功文件、不静默 fallback ✅（原子写自检 + 拒绝换格式）
- macOS 构建与 ABI smoke ✅（本机 x64 实测；Windows 列入阶段 12 收口）
- 冒烟测试入 ctest：cli_image_probe/verify/decode/benchmark（golden 文件驱动）

## 已知限制

- benchmark 的 decode 计时含每次整读 PIXL（内存 io）；NAS/冷缓存矩阵按协议在阶段 7 实测
- 适配器只支持 GBR（HQ/XQ）↔ PNG；Preview（YUV）转码需上层显式选矩阵，不在适配器猜测
