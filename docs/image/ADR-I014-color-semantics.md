# ADR-I014：O1 色彩、Alpha 和 metadata 语义收口

日期：2026-09-08  
状态：scalar/reference 完成；GPU、file explorer 专业查看和 ICC 深度比对 deferred

## 决策

1. `pixel_format`、`range`、`matrix`、`chroma_siting` 和位深只由 TPIC/IDSC
   解释，不能由 ICC、OCIO 或宿主默认值覆盖。
2. GBR 使用 identity matrix=0；YUV 禁止 matrix=0。未知 CICP 值、非法组合、
   非法 ICC profile 或带路径/脚本后缀的 OCIO 名称均为硬错误，不静默猜 sRGB。
3. 样本重建为 RGB 后，ICCP 是色彩表征权威；没有 ICC 时保留 CICP 语义。OCIO
   仅作为显式 colorspace 标识，若适配器提供解析后的 CICP assertion，必须与 IDSC
   一致。
4. PQ reference API 返回绝对 nits，HLG 返回 scene-relative linear，SDR 返回
   normalized linear。PQ 转 SDR 必须显式提供 display peak；tone mapping 不属于
   codec decode。
5. Alpha=0 的 RGB 在 scalar/reference 解码路径保留；premultiply 只能在明确的
   display boundary 执行，不能假设不可逆的 premultiply 可以恢复原 RGB。

## 实现

- `src/shared/media/color_decode.py`：`resolve_topos_color_semantics()`、transfer
  decode/display API、ICC/OCIO 校验、Alpha association 和 scalar alpha helper。
- `src/shared/color/types.py`：`ColorMetadataAuthority` 与 `AlphaAssociation`。
- `src/shared/media/topos_image_source.py`：`.toos` 打开时统一解析语义，并向
  `DecodedFrame.extra` 透传 `color_authority` / `ocio_colorspace`。
- `scripts/run_topos_color_reference.py`：生成 scalar reference vector report。

## 门禁与 deferred

`tests/media/test_topos_color_semantics.py` 覆盖 CICP/ICC/OCIO 冲突、PQ/HLG/SDR
传输函数、GBR/YUV 组合和 Alpha=0；`docs/image/reports/color_reference_o1.json`
记录可重跑的 scalar 结果。GPU parity、file explorer 专业显示、ICC tag 的
primaries/transfer 深度比对及 DeltaE2000 外部参考需要宿主/色彩库矩阵，不能在
当前纯 Python/native 单元门禁中伪装成已完成。
