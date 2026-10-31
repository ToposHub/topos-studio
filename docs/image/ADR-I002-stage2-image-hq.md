# ADR-I002：阶段 2 —— Image HQ（GBR 4:4:4 10-bit）图片层启用

日期：2026-09-05
状态：已接受
前置：ADR-I001（范围校正）

## 背景

计划书原阶段 2 假设 codec 尚无 GBR 4:4:4 10-bit 能力，要求从 codec spec 层启用。实施前实测确认上游已完成（profile 5 + pf=2 + minor=3，含 golden_codec_v1_gbr.bin、SIMD parity、门禁），按 ADR-I001 D7 收窄为图片层启用与验收复验。

## 决策与执行结果

| 原任务 | 收窄后执行 |
| --- | --- |
| codec spec 启用 GBR 4:4:4 10-bit | 上游已完成（capability_manifest.json profile 5）——不重复实现 |
| 冻结 plane order/identity matrix/full range/padding/stride | 图片层映射冻结：spec v0 §6.1（pixel_format=2 ⇔ channel_model=RGB、matrix=0、subsampling=0）；plane 顺序 = codec plane 0/1/2 = R/G/B |
| 量化矩阵调整 | 当时沿用 qmatrix_id 0/1；2026-09-07 已由上游新增图片专用 qmatrix_id=2，详见 ADR-I012 |
| RGB 测试集 + 通道偏色 + 多代 | 图片层 golden_image_v1_gbr10.bin（字节冻结）；envelope decode == 直接 codec decode 逐平面一致（无通道交换，test_gbr_hq_semantics 覆盖无 Alpha 与 A16 straight 两形态） |
| scalar/SIMD/golden parity | 上游门禁覆盖；图片层不引入第二条实现路径 |
| 全门禁 + 旧 10-bit YUV422 复验 | 2026-09-05 实测：34 个 debug 测试二进制全过 + 旧 transform/bitstream/codec/mov golden 无回归 |
| capability registry 只登记过门槛组合 | 新建 docs/image/capability_manifest.json（image 层单一真相源）：Preview/HQ = supported，XQ = reserved_stage3，Lossless = reserved |
| 真实素材质量基线 | **deferred 至阶段 7**（benchmark_protocol 冻结后于开发机实测；在此之前禁止引用任何质量数字） |

## 验收门槛核对（计划书阶段 2）

- 标量跨平台一致 / SIMD 与标量一致：上游门禁持续覆盖（本阶段无 codec 改动）✅
- RGB channel order、range、identity matrix 无交换或隐式 YUV 转换：reader GBR 语义测试（直传比对）✅
- 真实素材质量达到阶段 0 基线：deferred（阶段 7），已在能力清单标注 ⏸
- Alpha mode1 bit-exact、mode2 误差规则：上游已验；图片层 golden 复验 A16 straight ✅
- 旧 TPIC golden 字节 / MOV / YUV422 路径无回归：实测 ✅

## 后果

- `golden_image_v1_gbr10.bin` 入库，后续版本必须持续通过（向后兼容门禁）；
- 图片层能力登记以 docs/image/capability_manifest.json 为准，阶段 4 CLI/阶段 5 Python 集成只从它读取；
- Image XQ 在阶段 3 交付 12-bit 图片 golden 与门禁前，任何 UI/导出路径不得登记为可写 Profile。
