# RD2-07：V7-A packet writer

状态：**已实现，仍保持 opt-in/未发布能力位**（2026-09-08）。

本项把已经冻结的 V7-A TPLD 目录和 band segment 接成一个无分配的 packet
writer。调用方提供已编码的 5×slice payload，writer 负责：

- 计算固定目录大小和最终 `frame_packet_size`；
- 按 slice 几何验证 plane 覆盖、顺序和 block 边界；
- 生成 layer/slice/segment descriptor 的 canonical BE 表示；
- 把非空 segment 连续写入 payload 区，并计算每段 CRC 和 directory CRC；
- 以独立的 V7-A header codec 写出 major=7 的 53-byte frame header。

入口位于 `native/topos_codec/src/bitstream/layer_directory.h`：
`tc_v7_directory_write()`；V7-A header 的读写入口为
`tc_frame_header_encode_v7a()` / `tc_frame_header_decode_v7a()`。

## 安全和兼容边界

writer 不分配与 packet 同等大小的中间副本；size probe 会先返回所需容量，
容量不足时不修改输出缓冲。目录和 payload 均受现有 64 KiB directory、256 MiB
packet、checked arithmetic 和连续 range 规则约束。

V1–V6 的 `tc_frame_header_decode()` 和 `tc_packet_parse_structure()` 仍拒绝
major=7，因此旧 reader 不会把 TPLD 误读成旧 slice header。V7-A 的公共
reader/encoder、MOV/TPIM capability 以及 `TC_CODEC_CAP_V7A_BAND_DECODE` 仍由
后续 rollout gate 控制；本项不能单独证明 8K/12K 已达到 2K 负载。

## 验收

`unit_layer_directory` 已覆盖：

- size probe 和短输出缓冲的非破坏性错误；
- V7-A header encode/decode round-trip；
- 目录 parse、segment offset/CRC 和 payload round-trip；
- legacy reader 对有效 V7 packet 的明确拒绝。
