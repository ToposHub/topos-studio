# RD2-08：V7-A frame reader

状态：**已实现，公共 elementary-frame reader 已接入**（2026-09-08）。

`native/topos_codec/src/bitstream/v7_frame_codec.c` 现在提供目录驱动的 V7-A
帧 reader：

- 先用 V7-A 专用 header codec 和 TPLD parser 完成 bounded 结构校验；
- `max_band=0..4` 分别打开 B0、B0..B1、B0..B2、B0..B3、B0..B4；
- 未请求 segment 只做目录范围统计，不读取 payload、不计算 CRC、不初始化
  band entropy reader；
- 请求段经 CRC 和 band syntax 校验后合并为 slice 的 scan-order 系数，再转成
  现有 inverse transform 所需的 natural-order 系数，直接写 caller-owned plane；
- 使用现有 SIMD dispatch 的 dequant+inverse 内核，避免引入第二套重建数学。

这条路径支持源分辨率输出，也支持由公共 `tc_frame_decode_reduced()` 和
`tc_frame_decode_request()` 选择目标几何；缩放写回只生成目标 plane，不构造完整
源平面。公共 capability 已报告 V7-A band decode 可用，但 V7-A 仍不是 V7-B 的
≤2K spatial base，也还没有接入 MOV/TPIM 默认 writer。V7-A 只能证明“未请求
高频段可以 byte-level skip”，不能证明 8K/12K/16K 的熵码流工作集已经变成 2K
上限：它仍需遍历源 block 的目录与低频 segment。

## 验收

`unit_v7_frame_codec` 覆盖：

- V7-A writer → header/directory reader → pixel reconstruction round-trip；
- B0-only 与 full 的 `segments_read/skipped`、`bytes_read/skipped` 统计；
- query mode 不要求输出 plane；
- 公共 full/reduced/request 入口能够读取 V7-A，并按目标几何写出 plane；
- 未请求高频 segment 被破坏时 B0-only 仍成功，full decode 返回
  `TC_ERR_CHECKSUM_MISMATCH`；
- 旧 V1–V6 packet parser 对完整 V7 packet 仍明确返回
  `TC_ERR_UNSUPPORTED_VERSION`。
