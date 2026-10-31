# Topos 图片序列持久化导出实测

测试使用真实素材 `tests/test_videos/2K_h264.mp4`，从 1920×1080 源中取连续 8 帧，Release native codec，8 个 slice worker，Topos 422 High（profile 3、QP 58、Standard qmatrix）。旧路径是每帧启动一次 `toos` CLI；新路径是一次 `ToposImageSequenceEncoder`，每帧仍输出可独立打开的 `.toos`，但复用 ToposCodec、编码缓冲、slice 线程池和 rate state。序列写盘关闭了逐帧 `fsync`，保留临时文件 + 原子替换。

| 项目 | 旧单帧 CLI | 新持久化图片序列 | 提升 |
|---|---:|---:|---:|
| 编码 P50 | 83.14 ms（12.03 FPS） | 9.75 ms（102.54 FPS） | 稳态约 8.5× |
| 编码 P95 | 90.41 ms | 24.68 ms | — |
| 8 帧总编码时间 | 676.18 ms | 106.49 ms（含 open/close） | **6.35×** |
| 冷启动解码 P50 | 57.60 ms（17.36 FPS） | 11.67 ms（85.70 FPS） | **4.94×** |
| 8 帧总解码时间 | 460.68 ms | 73.33 ms（109.09 FPS） | **6.29×** |

两条路径的 8 帧总大小都是 **5,373,225 bytes**，平均 **671,653 bytes/帧（0.672 MB/帧）**，码率没有因为改成持久化序列而变化。每个新输出仍是完整 TPIM/TPIC 文件，可以单独 `probe`、`validate --deep` 和解码；取消时已经写出的序列帧会被清理。

解码提速来自持久化的 sequence reader：它直接使用进程级共享 `ToposImageCodec`，不再每帧创建 `ToposImageSource`、重复 open/probe，并保留有界预取。这里的解码对比是“旧的每帧 CLI 冷启动”与“新的连续序列读取”；如果两边都在同一个 Python 进程里直接调用 native decode，codec 内核本身已经是热的，提升会小很多。

实现入口：

- `ExportCodec.TOPOS_IMAGE_SEQUENCE`：Deliver 中新增 Topos Image Sequence，输出目录里的 `frame_0001.toos`、`frame_0002.toos`……
- `ToposImageSequenceEncoder`：复用 `ToposVideoEncoder` 的持久化编码上下文，保持 intra-only。
- `ToposImageCodec.write_packet()`：通过 native `tc_image_derive_idsc()` + `tc_image_write()` 写 TPIM envelope。
- `ToposImageSequenceSource._read_slot()`：直接走共享 image binding，减少每帧重复探测。

完整 JSON 数据见 [topos_image_sequence_export_speed_2026-09-07.json](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/image/topos_image_sequence_export_speed_2026-09-07.json>)。
