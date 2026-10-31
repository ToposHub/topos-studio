# 视频 V6/V7：当前帧内吞吐、容器和播放接线

状态：当前 all-intra 路线已实现并通过现有 native/Python 回归；inter 子路线
继续等待 `V3_V5_inter_deferred.md` 的启动条件。

## 当前已交付能力

- `decoder_ctx.c`、slice worker 和共享 worker budget 提供持久解码上下文，避免
  每帧创建线程；`ToposMediaSource` 对顺序读取提供有界、可取消的 read-ahead；
- `tc_movie_packet_batch` 和 Python `ToposMovieFile.packets_into_batch()` 支持
  批量取 packet，source 在跳转时清空预取缓存并回到单帧路径；
- `ToposMediaSource` 保持独立 sample 的 O(1) seek、sequential/random/reverse
  读取和 EOF 语义，输出不触碰 Qt/OpenGL；
- `ToposFrameExtractor` 直接包装 MOV TPIC packet 为 `.toos`，不 decode/reencode；
  因而当前 all-intra 任意 sample 都可零重编码抽帧；
- native MOV reader 校验 sample 长度/CRC 和 frame/container 语义，坏 slice 按
  concealment 规则交付，坏 header 或不一致配置拒绝打开/解码。

## 门禁命令

```text
ctest --test-dir native/topos_codec/build/release --output-on-failure \
  -R 'unit_mov|conformance_mov|unit_codec|unit_stress|unit_tpool_stats|unit_o4_pool_once'

python3 -m pytest -q \
  tests/media/test_topos_source.py \
  tests/media/test_topos_frame_extract.py \
  tests/media/test_topos_image_sequence.py
```

发布报告必须继续区分 probe、packet read、codec decode、color/resize 和显示编码；
不能把 Python/UI 时间写成 native codec FPS。未来 inter 路线接入时，V7 的抽帧
接口必须返回“从最近 IDR 解码”的实际成本，而不能复用当前零拷贝承诺。
