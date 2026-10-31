# M16.1 · 当前 HEAD 4K 解码 decode-only 离屏复测（首批基线）

> HEAD：f006503ff；日期：2026-10-24；宿主：Darwin x86_64（本 session 内并行跑测试，非安静机）。
> 命令：`.venv/bin/python scripts/bench_frame_budget.py --mode offscreen --generate-media 4k_h264 --frames 150 --warmup 20`
> 环境：QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 OPENBLAS_NUM_THREADS=1。

## 结果（telemetry summary）

| 素材 | decode_ms p50 | decode_ms p95 | total_ms p50 | decode_path |
|---|---|---|---|---|
| h264_3840x2160_8bit | 488.9 | 519.7 | 489.6 | macos_vt_iosurface_zero_copy ×170（0 CPU 回退） |
| hevc_2560x1440_10bit | 416.8 | 429.2 | 417.5 | （同 summary） |
| prores422hq_2560x1440_10bit | 227.8 | 771.9 | 228.5 | （同 summary） |

## 解读（不冒充结论）

- 解码路径为 macos_vt_iosurface_zero_copy，0 CPU 回退帧——路由本身健康。
- 本离屏 harness 的 decode_ms 是**逐帧同步等待**口径，与冻结基线的历史
  38.2ms（真实窗口管线等待口径）**不可直接对比**；且本机 session 内并行
  负载（大量测试运行）会推高绝对值。
- 首轮日志出现一次 "MacOSVtZeroCopySource: failed to reach target frame
  169 after reorder decode" 警告（VT 重排解码尾帧超时）——与 M04.1 修复
  的显示序索引相关路径值得在真实窗口复测时关注。

## 下一步（M16 剩余）

1. **真实窗口**冷首帧/seek/连续播放与 decode-only 矩阵复跑（需交互
   session/安静机——本轮离屏结果只作环境指纹，不作为"热点仍在/已消"
   的裁决）。
2. 锁等待/重复解码/命中率分域统计（M16.1）。
3. source_viewer/decoder worker 生命周期（M16.2）与冷启动/大工程
   （M16.3）基线。

## 回滚

纯测量，无代码改动。
