# T20 · 性能与生命周期证据

## 基准（纯数据路径，无 Qt/GPU/媒体解码）

- 机器：Mac Intel x86_64 · macOS 15.3.1 · 64GB · Python 3.12.14（.venv）
- 命令：`.venv/bin/python tests/edit/insights/bench_t20.py`（退出码 0）
- 原始数据：[bench.json](bench.json)（2026-10-24 实测）

| 规模 | capture | 索引构建 | 搜索命中 p50/p95 | 搜索落空 p50/p95 | 统计 | RSS 峰值 |
|---|---|---|---|---|---|---|
| 100 clip | ~0.004s | ~0.0005s | 0.12 / 0.13 ms | 0.13 / 0.14 ms | 0.8ms | 80 MB |
| 10 000 clip | 0.39s | 0.045s | 10.5 / 13.6 ms | 12.1 / 15.5 ms | 73ms | 145 MB |

10 万规模：按内存条件选择，默认不跑（`INSIGHTS_SCALE_100K=1` 开启）；
本机 64GB 满足，后续在 T21 验收时补一次带 UI 的实测。

## 结论

- 1 万实体：搜索 p95 约 14ms（纯数据路径，不触主线程媒体解码），
  首屏（capture+索引）< 0.5s——满足"不显著劣化前台播放"的量级；
- 索引缓存：同 content_hash 重捕获命中不重建；内容变化自动失效
  （`test_index_cache_hits_same_content_and_invalidates_new_capture`）；
- 生命周期：worker shutdown 有界等待（≤5s）、取消在检查点退出、
  临时产物只清自己目录、发布走 os.replace 不覆盖旧产物
  （`test_t20_runtime.py` 15 测试）。

## 未验项（如实登记）

- 播放中前台帧率的成对对比（需要真实宿主播放，T21/G1b 后补）；
- 反复开关窗口/工程后的信号连接回落（需要 T03 窗口实体，联测时补）；
- Windows 平台数字（W 线复验）。
