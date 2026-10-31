# Realtime Playback Benchmark Report

- **Timestamp**: 2026-06-15 00:55:22
- **Git SHA**: `ad1266c`
- **Overall**: ✅ PASS

## Scenarios

| Scenario | Video | Frames | FPS | p50 (ms) | p95 (ms) | master_req | placeholder | stale_gen | wrong_frame | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| source_decision |  | 60 | 22881.5 | 0.03 | 0.04 | 0 | 1.0 | 0 | 0 | engine source decision path（无需 GL context） |
| seek_stress |  | 50 | 21797.2 | 0.00 | 0.00 | 0 | 0 | 0 | 0 | 50 次随机 seek，generation 必须无 stale 堆积 |
| decode_real | 2K_DNxHR HQX 12-bit.mov | 60 | 100.0 | 7.05 | 11.23 | 0 | 0 | 0 | 0 | PyAV 顺序解码 2K_DNxHR HQX 12-bit.mov |
| decode_real | 2K_H265.mov | 60 | 77.7 | 7.26 | 9.38 | 0 | 0 | 0 | 0 | PyAV 顺序解码 2K_H265.mov |
| decode_real | 2K_h264.mp4 | 60 | 147.6 | 1.58 | 3.16 | 0 | 0 | 0 | 0 | PyAV 顺序解码 2K_h264.mp4 |
| decode_real | 2K_prores422HQ.mov | 60 | 102.3 | 6.29 | 8.15 | 0 | 0 | 0 | 0 | PyAV 顺序解码 2K_prores422HQ.mov |
| decode_real | 4K_DNxHR HQX 12-bit.mov | 60 | 42.6 | 13.47 | 18.92 | 0 | 0 | 0 | 0 | PyAV 顺序解码 4K_DNxHR HQX 12-bit.mov |
| decode_real | 4K_H264.mov | 60 | 49.6 | 3.05 | 3.95 | 0 | 0 | 0 | 0 | PyAV 顺序解码 4K_H264.mov |
| decode_real | 4K_H265.mov | 60 | 29.0 | 18.03 | 43.55 | 0 | 0 | 0 | 0 | PyAV 顺序解码 4K_H265.mov |
| decode_real | 4K_prores422HQ.mov | 60 | 33.5 | 14.72 | 19.63 | 0 | 0 | 0 | 0 | PyAV 顺序解码 4K_prores422HQ.mov |

## AVSync Decision Distribution

- evaluations: 600
- ignores: 303
- drops: 277
- holds: 6
- rebuffers: 14
- max_drift_ms: 200.0

## Gate Targets

| Metric | Target |
|---|---:|
| `wrong_source_frame` | 0 |
| `stale_generation` | 0 |
| `realtime_master_requests` | 0 |
