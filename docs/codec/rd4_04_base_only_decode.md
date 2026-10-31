# RD4-04 base-only 解码入口

`tc_v7b_frame_decode()` 的 `TC_V7B_DECODE_BASE_ONLY` 模式会先解析并验证有界
directory，再只校验/解码 base segment；residual segment 的 CRC、熵流和重建
均不触碰。普通独立 base packet 仍通过 `tc_base_frame_decode()` 兼容，二者都
不会创建或解码源分辨率输出平面。

V7-B 已接入 elementary-frame、reduced/AUTO_2K one-shot、持久 decoder context、
全 V7-B batch 和 MOV sample 结构验证。`tc_movie_packet_base()` 现在先读取
outer header + bounded directory，再按目录只读取 embedded base range；residual
payload 不进入 packet arena。`ToposMediaSource` 的 V7-B 固定比例/AUTO_2K 预览已
使用这个接口，并把 source-relative 比例映射为 base packet 上的 FULL 解码，避免
对 base 再次缩小。

批量预取使用 `tc_movie_packet_base_batch()`：每个 frame slot 独立完成同样的
范围读取并紧凑写入 arena；在该 API 不存在时，播放器会关闭 V7-B preview
read-ahead，避免旧的完整 sample batch 路径重新读入 residual。

统一 decode telemetry 会把 embedded base 内层 slice 与外层 segment 分开统计，
base-only 可见 `segments_skipped`，并且 `packet_bytes_read` 不包含被跳过的
residual 字节；MOV 读测试另以 read callback 计数验证没有读取 residual。RD6
仍需补齐跨平台 I/O、长稳和最终性能/质量门禁。
