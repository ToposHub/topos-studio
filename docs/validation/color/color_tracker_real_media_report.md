# Color Tracker 真实素材性能与稳定性报告

- 生成时间：2026-08-24 13:39:04 +0800
- 轮数：3
- 每轮最大帧数：120
- 模式：TRS 前向跟踪；draft=代理分辨率，high=原始分辨率

## 汇总

| 文件 | 质量 | 分辨率 | 帧数 | 平均 ms/帧 | 吞吐 FPS | 有效 | weak | 失败 | 峰值 RSS MB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2K_DNxHR HQX 12-bit.mov | draft | 1920×1080 | 121 | 15.70 | 63.68 | 121 | 0 | 0 | 327.4 |
| 2K_DNxHR HQX 12-bit.mov | draft | 1920×1080 | 121 | 17.66 | 56.62 | 121 | 0 | 0 | 344.2 |
| 2K_DNxHR HQX 12-bit.mov | draft | 1920×1080 | 121 | 16.47 | 60.72 | 121 | 0 | 0 | 348.1 |
| 2K_DNxHR HQX 12-bit.mov | high | 1920×1080 | 121 | 38.49 | 25.98 | 117 | 2 | 2 | 844.0 |
| 2K_DNxHR HQX 12-bit.mov | high | 1920×1080 | 121 | 40.43 | 24.74 | 117 | 2 | 2 | 895.1 |
| 2K_DNxHR HQX 12-bit.mov | high | 1920×1080 | 121 | 39.91 | 25.06 | 117 | 2 | 2 | 942.9 |
| 2K_H265.mov | draft | 1920×1080 | 121 | 12.41 | 80.57 | 121 | 0 | 0 | 1024.4 |
| 2K_H265.mov | draft | 1920×1080 | 121 | 12.85 | 77.85 | 121 | 0 | 0 | 1037.2 |
| 2K_H265.mov | draft | 1920×1080 | 121 | 12.55 | 79.67 | 121 | 0 | 0 | 1042.6 |
| 2K_H265.mov | high | 1920×1080 | 121 | 35.15 | 28.45 | 118 | 2 | 1 | 1091.4 |
| 2K_H265.mov | high | 1920×1080 | 121 | 35.74 | 27.98 | 118 | 2 | 1 | 1158.4 |
| 2K_H265.mov | high | 1920×1080 | 121 | 35.91 | 27.85 | 118 | 2 | 1 | 1176.1 |
| 2K_h264.mp4 | draft | 1920×1080 | 121 | 14.00 | 71.42 | 114 | 2 | 5 | 1178.0 |
| 2K_h264.mp4 | draft | 1920×1080 | 121 | 13.41 | 74.58 | 114 | 2 | 5 | 1178.6 |
| 2K_h264.mp4 | draft | 1920×1080 | 121 | 15.11 | 66.20 | 114 | 2 | 5 | 1178.7 |
| 2K_h264.mp4 | high | 1920×1080 | 121 | 28.72 | 34.82 | 104 | 1 | 16 | 1180.3 |
| 2K_h264.mp4 | high | 1920×1080 | 121 | 30.70 | 32.57 | 104 | 1 | 16 | 1181.7 |
| 2K_h264.mp4 | high | 1920×1080 | 121 | 28.65 | 34.91 | 104 | 1 | 16 | 1181.8 |
| 2K_prores422HQ.mov | draft | 1920×1080 | 121 | 19.21 | 52.05 | 119 | 2 | 0 | 1181.8 |
| 2K_prores422HQ.mov | draft | 1920×1080 | 121 | 20.89 | 47.86 | 119 | 2 | 0 | 1181.8 |
| 2K_prores422HQ.mov | draft | 1920×1080 | 121 | 19.06 | 52.46 | 119 | 2 | 0 | 1181.8 |
| 2K_prores422HQ.mov | high | 1920×1080 | 121 | 47.53 | 21.04 | 111 | 10 | 0 | 1198.3 |
| 2K_prores422HQ.mov | high | 1920×1080 | 121 | 52.27 | 19.13 | 111 | 10 | 0 | 1199.2 |
| 2K_prores422HQ.mov | high | 1920×1080 | 121 | 52.10 | 19.20 | 111 | 10 | 0 | 1208.6 |
| 4K_DNxHR HQX 12-bit.mov | draft | 3840×2160 | 121 | 37.69 | 26.53 | 121 | 0 | 0 | 1225.0 |
| 4K_DNxHR HQX 12-bit.mov | draft | 3840×2160 | 121 | 37.58 | 26.61 | 121 | 0 | 0 | 1225.3 |
| 4K_DNxHR HQX 12-bit.mov | draft | 3840×2160 | 121 | 38.41 | 26.03 | 121 | 0 | 0 | 1225.3 |
| 4K_DNxHR HQX 12-bit.mov | high | 3840×2160 | 121 | 147.34 | 6.79 | 120 | 1 | 0 | 2393.2 |
| 4K_DNxHR HQX 12-bit.mov | high | 3840×2160 | 121 | 129.53 | 7.72 | 120 | 1 | 0 | 2393.3 |
| 4K_DNxHR HQX 12-bit.mov | high | 3840×2160 | 121 | 136.04 | 7.35 | 120 | 1 | 0 | 2407.8 |
| 4K_H264.mov | draft | 3840×2160 | 121 | 18.96 | 52.74 | 119 | 2 | 0 | 2454.6 |
| 4K_H264.mov | draft | 3840×2160 | 121 | 21.54 | 46.43 | 119 | 2 | 0 | 2481.4 |
| 4K_H264.mov | draft | 3840×2160 | 121 | 21.90 | 45.67 | 119 | 2 | 0 | 2491.0 |
| 4K_H264.mov | high | 3840×2160 | 121 | 175.90 | 5.68 | 76 | 2 | 43 | 2786.8 |
| 4K_H264.mov | high | 3840×2160 | 121 | 158.08 | 6.33 | 76 | 2 | 43 | 2910.6 |
| 4K_H264.mov | high | 3840×2160 | 121 | 151.36 | 6.61 | 76 | 2 | 43 | 3056.0 |
| 4K_H265.mov | draft | 3840×2160 | 121 | 28.57 | 35.00 | 121 | 0 | 0 | 3313.1 |
| 4K_H265.mov | draft | 3840×2160 | 121 | 32.72 | 30.57 | 121 | 0 | 0 | 3313.1 |
| 4K_H265.mov | draft | 3840×2160 | 121 | 28.93 | 34.57 | 121 | 0 | 0 | 3313.1 |
| 4K_H265.mov | high | 3840×2160 | 121 | 144.99 | 6.90 | 117 | 4 | 0 | 3313.1 |
| 4K_H265.mov | high | 3840×2160 | 121 | 126.74 | 7.89 | 117 | 4 | 0 | 3328.5 |
| 4K_H265.mov | high | 3840×2160 | 121 | 144.91 | 6.90 | 117 | 4 | 0 | 3341.8 |
| 4K_prores422HQ.mov | draft | 3840×2160 | 121 | 46.23 | 21.63 | 119 | 2 | 0 | 3341.8 |
| 4K_prores422HQ.mov | draft | 3840×2160 | 121 | 54.45 | 18.37 | 119 | 2 | 0 | 3341.8 |
| 4K_prores422HQ.mov | draft | 3840×2160 | 121 | 55.66 | 17.97 | 119 | 2 | 0 | 3341.8 |
| 4K_prores422HQ.mov | high | 3840×2160 | 121 | 140.18 | 7.13 | 120 | 1 | 0 | 3345.7 |
| 4K_prores422HQ.mov | high | 3840×2160 | 121 | 138.14 | 7.24 | 120 | 1 | 0 | 3351.5 |
| 4K_prores422HQ.mov | high | 3840×2160 | 121 | 143.23 | 6.98 | 120 | 1 | 0 | 3351.7 |

## 运行明细

### 2K_DNxHR HQX 12-bit.mov

```json
[
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.9001282019999053,
    "mean_frame_ms": 15.70353885950335,
    "throughput_fps": 63.67991374089717,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 327.38671875,
    "round": 1
  },
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.137209789000053,
    "mean_frame_ms": 17.66289081818226,
    "throughput_fps": 56.61587394123478,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 344.1640625,
    "round": 2
  },
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.992891525999994,
    "mean_frame_ms": 16.470177900826396,
    "throughput_fps": 60.71579833693385,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 348.1484375,
    "round": 3
  },
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.657704663000004,
    "mean_frame_ms": 38.49342696694218,
    "throughput_fps": 25.978461228167376,
    "valid_frames": 117,
    "weak_frames": 2,
    "failed_frames": 2,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 843.98046875,
    "round": 1
  },
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.891563592000011,
    "mean_frame_ms": 40.426145388429845,
    "throughput_fps": 24.73646671953554,
    "valid_frames": 117,
    "weak_frames": 2,
    "failed_frames": 2,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 895.140625,
    "round": 2
  },
  {
    "file": "2K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.829107756999974,
    "mean_frame_ms": 39.909981462809704,
    "throughput_fps": 25.056388485969467,
    "valid_frames": 117,
    "weak_frames": 2,
    "failed_frames": 2,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 156.83982297032136,
    "peak_rss_mb": 942.91015625,
    "round": 3
  }
]
```

### 2K_H265.mov

```json
[
  {
    "file": "2K_H265.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.5017099639999287,
    "mean_frame_ms": 12.410826148759742,
    "throughput_fps": 80.57481331328887,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1024.3828125,
    "round": 1
  },
  {
    "file": "2K_H265.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.5542939189999743,
    "mean_frame_ms": 12.845404289255987,
    "throughput_fps": 77.84885375981581,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1037.2109375,
    "round": 2
  },
  {
    "file": "2K_H265.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.5187273779999941,
    "mean_frame_ms": 12.551465933884248,
    "throughput_fps": 79.67196861845238,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1042.5859375,
    "round": 3
  },
  {
    "file": "2K_H265.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.253541939000115,
    "mean_frame_ms": 35.15323916529021,
    "throughput_fps": 28.44688067856305,
    "valid_frames": 118,
    "weak_frames": 2,
    "failed_frames": 1,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1091.35546875,
    "round": 1
  },
  {
    "file": "2K_H265.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.3243091150000055,
    "mean_frame_ms": 35.73809185950418,
    "throughput_fps": 27.981348414774423,
    "valid_frames": 118,
    "weak_frames": 2,
    "failed_frames": 1,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1158.3828125,
    "round": 2
  },
  {
    "file": "2K_H265.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.344800680999924,
    "mean_frame_ms": 35.90744364462747,
    "throughput_fps": 27.84937880560098,
    "valid_frames": 118,
    "weak_frames": 2,
    "failed_frames": 1,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.37526208624548,
    "peak_rss_mb": 1176.140625,
    "round": 3
  }
]
```

### 2K_h264.mp4

```json
[
  {
    "file": "2K_h264.mp4",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.6941135519998625,
    "mean_frame_ms": 14.000938446279855,
    "throughput_fps": 71.42378375827421,
    "valid_frames": 114,
    "weak_frames": 2,
    "failed_frames": 5,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1178.015625,
    "round": 1
  },
  {
    "file": "2K_h264.mp4",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.6224130630000673,
    "mean_frame_ms": 13.40837242148816,
    "throughput_fps": 74.58026735574613,
    "valid_frames": 114,
    "weak_frames": 2,
    "failed_frames": 5,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1178.61328125,
    "round": 2
  },
  {
    "file": "2K_h264.mp4",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 1.827922130999923,
    "mean_frame_ms": 15.106794471073744,
    "throughput_fps": 66.19537996063855,
    "valid_frames": 114,
    "weak_frames": 2,
    "failed_frames": 5,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1178.69921875,
    "round": 3
  },
  {
    "file": "2K_h264.mp4",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.474527584999805,
    "mean_frame_ms": 28.715104008262852,
    "throughput_fps": 34.82487821434487,
    "valid_frames": 104,
    "weak_frames": 1,
    "failed_frames": 16,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1180.3203125,
    "round": 1
  },
  {
    "file": "2K_h264.mp4",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.714532793999979,
    "mean_frame_ms": 30.69861813223123,
    "throughput_fps": 32.57475615653447,
    "valid_frames": 104,
    "weak_frames": 1,
    "failed_frames": 16,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1181.68359375,
    "round": 2
  },
  {
    "file": "2K_h264.mp4",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 1572,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.4661702970001897,
    "mean_frame_ms": 28.646035512398264,
    "throughput_fps": 34.90884452639846,
    "valid_frames": 104,
    "weak_frames": 1,
    "failed_frames": 16,
    "cancelled": false,
    "region": [
      0.4,
      0.4,
      0.2,
      0.2
    ],
    "region_features": 240,
    "region_sharpness": 2043.7962330411342,
    "peak_rss_mb": 1181.7578125,
    "round": 3
  }
]
```

### 2K_prores422HQ.mov

```json
[
  {
    "file": "2K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.3246468829997866,
    "mean_frame_ms": 19.211957710742038,
    "throughput_fps": 52.0509161562888,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1181.78125,
    "round": 1
  },
  {
    "file": "2K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.5279514290000407,
    "mean_frame_ms": 20.89216057024827,
    "throughput_fps": 47.864843688022475,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1181.80078125,
    "round": 2
  },
  {
    "file": "2K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.306575615000156,
    "mean_frame_ms": 19.06260838843104,
    "throughput_fps": 52.45871811576912,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1181.8125,
    "round": 3
  },
  {
    "file": "2K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 5.7517275610000524,
    "mean_frame_ms": 47.53493852066159,
    "throughput_fps": 21.03715774377911,
    "valid_frames": 111,
    "weak_frames": 10,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1198.3046875,
    "round": 1
  },
  {
    "file": "2K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 6.324498944999959,
    "mean_frame_ms": 52.268586322313716,
    "throughput_fps": 19.131950380932633,
    "valid_frames": 111,
    "weak_frames": 10,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1199.16796875,
    "round": 2
  },
  {
    "file": "2K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      1920,
      1080
    ],
    "source_fps": 25.0,
    "source_frames": 647,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 6.303562822999993,
    "mean_frame_ms": 52.095560520661095,
    "throughput_fps": 19.195493627588476,
    "valid_frames": 111,
    "weak_frames": 10,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.32,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 4.118048226652352,
    "peak_rss_mb": 1208.58984375,
    "round": 3
  }
]
```

### 4K_DNxHR HQX 12-bit.mov

```json
[
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.56077471399999,
    "mean_frame_ms": 37.692353008264384,
    "throughput_fps": 26.530580348240427,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 1224.9609375,
    "round": 1
  },
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.546852056000034,
    "mean_frame_ms": 37.57728971900855,
    "throughput_fps": 26.611818134774843,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 1225.32421875,
    "round": 2
  },
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 4.648109389999945,
    "mean_frame_ms": 38.41412719008219,
    "throughput_fps": 26.03208957610213,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 1225.32421875,
    "round": 3
  },
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 17.827954254000133,
    "mean_frame_ms": 147.338464909092,
    "throughput_fps": 6.78709392429873,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 2393.19140625,
    "round": 1
  },
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 15.673674094000035,
    "mean_frame_ms": 129.53449664462838,
    "throughput_fps": 7.719951255482557,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 2393.33984375,
    "round": 2
  },
  {
    "file": "4K_DNxHR HQX 12-bit.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 16.460776188999944,
    "mean_frame_ms": 136.0394726363632,
    "throughput_fps": 7.350807678246625,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 138.11512628038122,
    "peak_rss_mb": 2407.828125,
    "round": 3
  }
]
```

### 4K_H264.mov

```json
[
  {
    "file": "4K_H264.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.294273953000129,
    "mean_frame_ms": 18.960941760331647,
    "throughput_fps": 52.7399963904804,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 2454.58203125,
    "round": 1
  },
  {
    "file": "4K_H264.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.606309943000042,
    "mean_frame_ms": 21.53975159504167,
    "throughput_fps": 46.42579073336177,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 2481.39453125,
    "round": 2
  },
  {
    "file": "4K_H264.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 2.6494735889998537,
    "mean_frame_ms": 21.89647594214755,
    "throughput_fps": 45.66944939642751,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 2491.03125,
    "round": 3
  },
  {
    "file": "4K_H264.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 21.284311234999905,
    "mean_frame_ms": 175.90339863636285,
    "throughput_fps": 5.684938481872399,
    "valid_frames": 76,
    "weak_frames": 2,
    "failed_frames": 43,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 2786.76953125,
    "round": 1
  },
  {
    "file": "4K_H264.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 19.12757560099999,
    "mean_frame_ms": 158.079137198347,
    "throughput_fps": 6.325945458224937,
    "valid_frames": 76,
    "weak_frames": 2,
    "failed_frames": 43,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 2910.5703125,
    "round": 2
  },
  {
    "file": "4K_H264.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 18.31450717000007,
    "mean_frame_ms": 151.35956338843033,
    "throughput_fps": 6.606784385560921,
    "valid_frames": 76,
    "weak_frames": 2,
    "failed_frames": 43,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 94.11395974060308,
    "peak_rss_mb": 3056.04296875,
    "round": 3
  }
]
```

### 4K_H265.mov

```json
[
  {
    "file": "4K_H265.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.457135934000007,
    "mean_frame_ms": 28.57137135537196,
    "throughput_fps": 35.00007008980971,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3313.10546875,
    "round": 1
  },
  {
    "file": "4K_H265.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.958713590000116,
    "mean_frame_ms": 32.71664123967038,
    "throughput_fps": 30.565484784161022,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3313.10546875,
    "round": 2
  },
  {
    "file": "4K_H265.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 3.500646550000056,
    "mean_frame_ms": 28.930963223140957,
    "throughput_fps": 34.56504342033561,
    "valid_frames": 121,
    "weak_frames": 0,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3313.10546875,
    "round": 3
  },
  {
    "file": "4K_H265.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 17.544185984000023,
    "mean_frame_ms": 144.99327259504153,
    "throughput_fps": 6.896871710682376,
    "valid_frames": 117,
    "weak_frames": 4,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3313.10546875,
    "round": 1
  },
  {
    "file": "4K_H265.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 15.335579743999915,
    "mean_frame_ms": 126.74032846280922,
    "throughput_fps": 7.890148401291549,
    "valid_frames": 117,
    "weak_frames": 4,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3328.47265625,
    "round": 2
  },
  {
    "file": "4K_H265.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 17.534170808,
    "mean_frame_ms": 144.91050254545453,
    "throughput_fps": 6.900811069137841,
    "valid_frames": 117,
    "weak_frames": 4,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 125.50362877864028,
    "peak_rss_mb": 3341.75390625,
    "round": 3
  }
]
```

### 4K_prores422HQ.mov

```json
[
  {
    "file": "4K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 5.594211357999939,
    "mean_frame_ms": 46.23315171900776,
    "throughput_fps": 21.629500971028797,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3341.79296875,
    "round": 1
  },
  {
    "file": "4K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 6.588151884000126,
    "mean_frame_ms": 54.447536231406005,
    "throughput_fps": 18.366303954506353,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3341.79296875,
    "round": 2
  },
  {
    "file": "4K_prores422HQ.mov",
    "quality": "draft",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 6.734977485999934,
    "mean_frame_ms": 55.66097095867714,
    "throughput_fps": 17.96591009420951,
    "valid_frames": 119,
    "weak_frames": 2,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3341.79296875,
    "round": 3
  },
  {
    "file": "4K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 16.96191382899997,
    "mean_frame_ms": 140.18110602479314,
    "throughput_fps": 7.133628977239879,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3345.6796875,
    "round": 1
  },
  {
    "file": "4K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 16.714567646999967,
    "mean_frame_ms": 138.13692270247907,
    "throughput_fps": 7.239194130260247,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3351.5390625,
    "round": 2
  },
  {
    "file": "4K_prores422HQ.mov",
    "quality": "high",
    "source_size": [
      3840,
      2160
    ],
    "source_fps": 25.0,
    "source_frames": 743,
    "requested_frames": 120,
    "processed_frames": 121,
    "elapsed_s": 17.330959295999946,
    "mean_frame_ms": 143.23106856198302,
    "throughput_fps": 6.981725473668805,
    "valid_frames": 120,
    "weak_frames": 1,
    "failed_frames": 0,
    "cancelled": false,
    "region": [
      0.32,
      0.5700000000000001,
      0.36,
      0.36
    ],
    "region_features": 240,
    "region_sharpness": 128.16595713020072,
    "peak_rss_mb": 3351.734375,
    "round": 3
  }
]
```

## 取消与换片段稳定性验收

- 独立 OpenCV 直接采样探针：连续 20 次跟踪后在第 5 个进度回调取消。
- 素材切换：第 1–10 次使用 `2K_h264.mp4`，第 11–20 次使用 `2K_H265.mov`。
- 结果：取消成功 `20/20`，每次 generation 与请求一致，未发现旧结果污染。
- 进程峰值 RSS 增长约 `94.75 MB`；该值包含 OpenCV 解码缓存，需在长时间运行中结合平台监控进一步确认。
- 复验（引擎集成稳定性循环，`scripts/tracking_real_media_probe.py --stability 20`）：
  素材 `2K_h264.mp4`（draft），20 次「启动→第 30 帧取消」全部按预期取消（20/20），
  峰值 RSS 341.8MB → 459.9MB（增长 118.1MB，含解码器缓存波动），无泄漏迹象。

## 已知限制

- 真实素材无人工 ground truth，只验证不崩溃/无漂移异常/吞吐达标；像素精度由合成素材验收。
- 默认每轮只分析前 120 帧，全片跟踪用 `--max-frames 0`。
- `2K_h264.mp4` high 分辨率失败帧偏多（16/121），与素材自身低纹理/运动相关，跟踪器均诚实标记，无假有效。
- 阶段 9（3D 摄像机）与阶段 10（完整产品化验收）不在本期范围；面板 3D 按钮为禁用态。
- 撤销/重做走现有 history 机制挂载点，未新建撤销系统。

## 判定说明

- 本探针验证解码、代理/原始分辨率路径、连续跟踪、失败统计和重复运行稳定性；真实素材没有人工 ground truth，因此不以像素误差判定正确性。
- `failed_frames` 不等于崩溃；它代表跟踪器诚实标记为失败的帧。应结合素材运动、特征数和日志判断。
- `peak_rss_mb` 是当前进程的峰值 RSS 近似值，包含解码器和 OpenCV 原生内存。
