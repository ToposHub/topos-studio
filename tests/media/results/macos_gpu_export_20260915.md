# macOS Metal → VideoToolbox timeline export benchmark

- Status: `passed`
- Generated: `2026-09-15T08:39:34.691308+00:00`
- Commit: `c1183860f0abc503768bdb7003fd624af1676b52`
- Full-GPU claim: `False`
- Claim reason: `gpu_time_unmeasured:decode`

## Scope

Explicit `VideoToolboxGpuExportBridge` through `TimelineExporter`; single H.264 video track, no audio, no subtitles, Metal NV12 output.

## Input / output

- Input: `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/test_videos/2K_h264.mp4`
- Input SHA-256: `3d420027cb4b6afcff11e5cc7b3510ddbb019de732ee403d21f6f40b3f1af04f`
- Input: `h264` / `yuv420p` `1920x1080` @ `25.000` fps
- Requested frames: `480`
- Output: `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/bench_out/macos_gpu_export_20260915/2K_h264_480frames.mp4`
- Output: `h264` / `yuv420p` `1920x1080`, decoded `480` frames
- Native VT packets: `480`

## Timing

- Wall time: `3.948` s
- End-to-end throughput: `121.593` fps

## ExportGpuTrail

| Stage | Backend | Samples | Mean GPU ms | P50 GPU ms | P95 GPU ms | CPU upload | CPU download |
|---|---|---:|---:|---:|---:|---:|---:|
| `decode` | `unknown` | 480 | None | None | None | 0 | 0 |
| `encode_submit` | `videotoolbox` | 480 | None | None | None | 0 | 0 |
| `render` | `metal` | 480 | None | None | None | 0 | 0 |
| `yuv_conversion` | `metal` | 480 | None | None | None | 0 | 0 |

- Trail CPU upload bytes: `0`
- Trail CPU download bytes: `0`
- Fallback reason: ``

## Boundary

This report proves the real multi-frame bridge and playable output. It does not claim full-chain GPU completion when decode or native VideoToolbox encode timestamps are unavailable; the claim remains fail-closed in the JSON evidence.
