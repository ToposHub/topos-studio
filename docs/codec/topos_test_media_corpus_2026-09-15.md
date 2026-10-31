# Topos 真实测试素材集（2026-09-15）

`tests/test_videos` 是本机测试素材目录，已按当前 Topos 编码规范重新生成两组短片：

- 2K：1920×1080，96 帧，25 fps（3.84 秒）；
- 4K：3840×2160，96 帧，25 fps（3.84 秒）；
- 422 Proxy/LT/Standard/HQ 使用对应分辨率的 ProRes 422 HQ 源；
- 4444 使用 DNxHR 444 10-bit 源，4444 XQ 使用 DNxHR 444 12-bit 源；
- 编码走产品 `ToposVideoEncoder`、默认 `rans2` 熵模式、MOV 容器和档位码控；
- 每个文件都已检查 MOV 元数据，并成功解码首帧和末帧。

生成或重新生成：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
  .venv/bin/python tools/generate_topos_test_media.py --frames 96 --threads 16
```

生成脚本只保留当前规范的输出；旧的 `4K_prores422HQ.topos` 已删除。为了兼容尚未迁移的基准工具，`2K_topos.mov` 和 `4K_topos.mov` 是新生成 HQ 文件的副本，不再是旧 Legacy 码流。

## 输出文件

| 分辨率 | 档位 | 文件 | 位深/采样 | 实测码率 |
|---|---|---|---|---:|
| 2K | Proxy | `2K_topos_proxy.mov` | 4:2:2 10-bit | 15.1 Mbps |
| 2K | LT | `2K_topos_lt.mov` | 4:2:2 10-bit | 50.6 Mbps |
| 2K | Standard | `2K_topos_standard.mov` | 4:2:2 10-bit | 79.9 Mbps |
| 2K | HQ | `2K_topos_hq.mov` | 4:2:2 10-bit | 133.9 Mbps |
| 2K | 4444 | `2K_topos_4444.mov` | 4:4:4 10-bit | 221.2 Mbps |
| 2K | 4444 XQ | `2K_topos_4444xq.mov` | 4:4:4 12-bit | 329.7 Mbps |
| 4K | Proxy | `4K_topos_proxy.mov` | 4:2:2 10-bit | 60.6 Mbps |
| 4K | LT | `4K_topos_lt.mov` | 4:2:2 10-bit | 200.1 Mbps |
| 4K | Standard | `4K_topos_standard.mov` | 4:2:2 10-bit | 319.0 Mbps |
| 4K | HQ | `4K_topos_hq.mov` | 4:2:2 10-bit | 530.8 Mbps |
| 4K | 4444 | `4K_topos_4444.mov` | 4:4:4 10-bit | 884.7 Mbps |
| 4K | 4444 XQ | `4K_topos_4444xq.mov` | 4:4:4 12-bit | 1299.9 Mbps |

生成后的逐文件 SHA-256、包大小、Profile、首末包大小和验证结果保存在本机的 `tests/test_videos/topos_test_media_manifest.json` 中。该目录按项目约定被 `.gitignore` 忽略；需要新环境时运行上面的命令即可复现。
