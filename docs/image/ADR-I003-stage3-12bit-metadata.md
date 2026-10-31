# ADR-I003：阶段 3 —— Image XQ（12-bit）与元数据受限读写

日期：2026-09-05
状态：已接受
前置：ADR-I001（范围校正）、ADR-I002（阶段 2）

## 背景

计划书原阶段 3 假设 12-bit 需从 codec 层启用。实测上游已实现（bd=12，v1.2 枚举扩展，golden_codec_v1_bd12.bin + golden_codec_v1_gbr.bin 12-bit 记录），按 ADR-I001 D8 收窄为图片层启用与元数据能力交付。

## 决策与执行结果

| 项 | 决策/执行 |
| --- | --- |
| 12-bit 图片档 | `golden_image_v1_gbr12.bin` 入库（GBR 4:4:4 12-bit，Image XQ）；capability_manifest.json 中 XQ 状态提升 reserved_stage3 → supported |
| 位深保持 | 测试锁定 IDSC.valid/container_bit_depth=12、内层 header bd=12、解码值域 ≤4095（不被中间层降为 10/8-bit），envelope decode == 直接 codec decode |
| 元数据读取 ABI | 新增 `tc_image_read_metadata()`：按 chunk 定位读取（分块、有界），支持尺寸查询与 BUFFER_TOO_SMALL 语义 |
| 元数据受限校验 | ICCP：≥132B、'acsp' 签名、colorSpace 与通道模型相符（RGB ⇔ 'RGB '）；OCIO：可打印 UTF-8 名称，禁止控制字符/路径分隔符(/ \\ :)/动态库后缀；容忍单个结尾 NUL；XMP/EXIF/THMB/HASH：长度硬上限 + 原样字节（不作语义解析） |
| 引用-存在一致性 | `iccp_ref ⇔ ICCP chunk`、`ocio_ref ⇔ OCIO chunk` 双向强制（写端拒绝产出、读端 probe 拒绝），落实"同一语义单一权威" |
| 冲突诊断 | ICC↔通道模型不符、OCIO 路径形态 → `TC_IMG_ERR_METADATA_CONFLICT` |
| 元数据 round-trip | 未知 optional chunk 字节级保留；golden_image_v1_meta.bin（ICCP/OCIO/XMP/EXIF/THMB/HASH 全集）冻结为保留基准 |
| 深度 ICC primaries↔CICP 色域比对 | **v1.1 扩展（推迟）**：需完整 ICC tag 解析器；v1 只做结构级校验并在文档明示 |

## 验收门槛核对（计划书阶段 3）

- UBSan 无有符号溢出/未定义移位：UBSan 全过（含 12-bit 路径）✅
- 10-bit v1 文件仍可读：全部旧 golden + Preview/HQ 用例通过 ✅
- 12-bit 不被中间层降位：值域与位深测试锁定 ✅
- ICC/OCIO/CICP 往返不静默改写：字节级 round-trip + 受限校验 ✅（深度比对推迟已声明）
- metadata 长度/嵌套/字符串/分配硬上限：§10 上限 + 校验器 + fuzz/ASan 门禁 ✅

## 门禁记录（2026-09-05）

- debug：reader/writer/container 全过；golden 5 用例（v1/alpha/gbr10/gbr12/meta）gen→check 字节一致
- ASan：3 测试 + fuzz 512 runs 全过（其中 ASan 抓到并修复测试侧 `uint16_t*[3]` 未满足 codec 4 槽契约的越界——属测试 bug，库无越界）
- UBSan：3 测试全过
- 附带：OCIO 校验器允许单个结尾 NUL（C 字符串直写场景），其余内容规则不变
