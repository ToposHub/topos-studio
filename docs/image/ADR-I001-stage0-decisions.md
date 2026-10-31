# ADR-I001：Topos Image 阶段 0 决策冻结

日期：2026-09-04
状态：已接受（阶段 0 交付物）
输入：`docs/Topos Image（TPIC）静态图像格式与序列管线开发计划书.txt`
关联：`docs/image/topos_image_file_spec_v0.md`

## 背景与现状校正

计划书起草时假设 codec 主路径仅为"YUV 4:2:2 10-bit"。实施前实测（2026-09-04，Explore 全量调研 + capability_manifest.json）确认 **codec 实际能力已超过计划书假设**：

- `GBR 4:4:4`（pf=2）、`YUV 4:4:4`（pf=1）、12-bit（bd=12）均已实现并有 golden（`golden_codec_v1_gbr.bin`、`golden_codec_v1_bd12.bin`、`golden_codec_v1_pf444.bin`）；
- profile 3（Standard）/5（Pro444）/6（Extreme）经 `tc_query_support()` 协商，交叉规则见 `src/bitstream/frame_header.c:67-125`；
- CRC-32 IEEE 802.3（`src/common/crc32.c`）与 checked arithmetic（`src/common/checked.h`）可直接复用；
- `topos_inspect` 等 CLI、ASan/UBSan/fuzz/门禁（`run_tests.sh`）、ctypes 绑定（`src/shared/codec/topos_binding.py`）均已存在。

因此对计划书做如下**范围调整**（不改变其原则）：

## 决策

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | 扩展名 `.tpic` 唯一；Magic `TPIM`；`.topos` 永久工程包 | 计划书 §2；避免与工程路由冲突。**已被取代（superseded-by ADR-I008，2026-09-05：扩展名迁移 `.toos`，`.tpic` 退役）** |
| D2 | Preamble=64B、Directory Entry=32B，逐 offset 冻结于 spec v0 §3/§4 | 修正草案 63B 缺陷；显式字段便于 checked 解析 |
| D3 | IDSC=128B 固定布局，含 payload_header_crc32 绑定摘要；IDSC↔PIXL 十项交叉校验（spec §6.1） | 同一语义单一权威；冲突硬错误 `TC_IMG_ERR_CHUNK_CONFLICT` |
| D4 | 8K/大图采用计划书策略 1（≤8192、≤256MiB），预留 TILE/TDIR FourCC | 未做最坏审计前不放宽上限 |
| D5 | image 错误码 = codec TC_ERR_*（-1..-16）+ TC_IMG_*（-100..-107） | 不动已冻结的 codec ABI；错误文本走 `tc_image_status_message` |
| D6 | 色彩优先级冻结：codec 数值解释 > ICCP（权威表征）> CICP 摘要 > OCIO 标识；矛盾硬错误，缺失返回 unspecified | 计划书 §4.6；禁止静默 sRGB 猜测 |
| D7 | 原计划阶段 2（GBR444 10-bit）**收窄**：codec 数学已由上游完成并过门禁，image 层只做「envelope 内 RGB golden + 能力登记 + 回归复验」，不重写 codec | 能力真相源 = capability_manifest.json；禁止重复实现 |
| D8 | 原计划阶段 3（12-bit）**同理收窄**：仅图片层启用（XQ profile 2、golden、ICCP/OCIO/XMP/EXIF 受限读写） | 同上 |
| D9 | Image Lossless（profile 3）reserved，v1 拒绝；"无损"仅用于描述 Alpha mode 1 **【superseded-by：2026-09-13 TRAW 拍板——reserved 值 3 改派 Image RAW（TRAW），Lossless 档移除，无损语义由 RAW Linear 模式承载；见 docs/image/capability_manifest.json profiles."3" 与 ADR-I015】** | 计划书 §3.4 用词纪律 |
| D10 | 裸 TPIC packet 测试后缀 `.tpkt`；`topos_inspect` 帮助文本迁移；`.tpic` 只对应 TPIM | 消除双 Magic 歧义 |
| D11 | Alpha=0 时 RGB 逐字保留；premultiply 转换仅在明确线性边界由调用方执行 | 文件层不做隐式转换 |
| D12 | metadata chunk 长度硬上限：ICCP 8MiB / XMP 16MiB / EXIF 16MiB / OCIO 4KiB / THMB 4MiB | metadata bomb 防护 |
| D13 | THMB 不参与主图真实性校验；HASH（SHA-256）仅资产去重 | 单一权威原则 |
| D14 | 原阶段 9/10/11（Nuke/Photoshop/AE）维持"后续独立立项"，不计入 v1 完成 | 计划书状态表原文 |

## 阶段映射（调整后）

| 计划阶段 | 调整 |
| --- | --- |
| 0 | 本文 + spec v0 + benchmark protocol + test vector manifest + risk register |
| 1 | 照原样：TPIM parser/writer + 安全基础设施（复用 tc_crc32/checked.h） |
| 2 | 收窄：image 层 GBR10 golden + 能力登记 + 旧门禁回归复验 |
| 3 | 收窄：12-bit image golden + metadata chunk 受限读写 |
| 4 | 照原样：完整 C ABI + `tpic` CLI + 原子写 |
| 5-7 | 照原样：Topos Color 集成、序列、性能（测量优先） |
| 8 | OIIO 插件（环境无 OIIO SDK 时仅交付可构建的插件源 + 文档） |
| 9-11 | 独立立项（不在本仓库 v1 范围） |
| 12 | 收口：全门禁、跨平台构建审计、SDK 文档 |

## 后果

- codec ABI（v2）与既有 golden 零改动，视频路径零回归风险；
- 图片层新增符号全部位于 `topos_image.h` 与 `src/image/`，`check_exports.py` 符号钉死需同步扩展；
- "350 fps / 5–10× PNG" 等未测量数字继续被禁止；一切性能结论出自 `benchmark_protocol.md`。
