# P5-2 Standard Material Matrix

本文档定义 Topos Color 视频优化方案的测试素材矩阵，涵盖视频解码管线的所有关键场景。

---

## 素材矩阵概览

| 类型 | 编码 | 色深 | 色度采样 | 分辨率 | 测试目标 |
|---|---|---|---|---|---|
| H.264 8-bit | libx264 | 8-bit | 4:2:0 | 1080p/4K | 硬解 + proxy |
| H.265 10-bit | libx265 | 10-bit | 4:2:0 | 4K | 硬解/CPU fallback |
| ProRes 422 HQ | prores_ks | 10-bit | 4:2:2 | 1080p/4K | CPU/proxy/v3 不降 |
| ProRes 4444 | prores_ks | 12-bit | 4:4:4 | 1080p/4K | alpha/高位深 fallback |
| DNxHR HQX | dnxhd | 10-bit | 4:2:2 | 4K | intra CPU/proxy |
| NV12 | raw | 8-bit | 4:2:0 | 1080p | YUV shader 正确性 |
| P010 | raw | 10-bit | 4:2:0 | 1080p | HDR shader 正确性 |

---

## 详细素材规格

### 1. H.264 8-bit 4:2:0

#### h264_8bit_420_1080p

```
ID: h264_8bit_420_1080p
Name: H.264 8-bit 4:2:0 1080p
File: p52/h264_8bit_420_1080p.mp4

Resolution: 1920 x 1080
FPS: 29.97 (30000/1001)
Duration: 10.0s (300 frames)
Bit Depth: 8-bit
Pixel Format: yuv420p
Chroma: 4:2:0

Codec: H.264 High Profile Level 4.1
GOP: 30 frames, B=3, Ref=4

Color Metadata:
- Color Space: bt709
- Color Primaries: bt709
- Color Transfer: bt709
- Color Range: tv (limited)

Performance Targets:
- HW Decode FPS: >= 60 fps
- Proxy Decode FPS: >= 120 fps (scale=0.5)
- Seek Time: < 50 ms
- Open Time: < 200 ms
```

#### h264_8bit_420_4k

```
ID: h264_8bit_420_4k
Name: H.264 8-bit 4:2:0 4K
File: p52/h264_8bit_420_4k.mp4

Resolution: 3840 x 2160
FPS: 30.0
Duration: 10.0s (300 frames)
Bit Depth: 8-bit
Pixel Format: yuv420p
Chroma: 4:2:0

Codec: H.264 High Profile Level 5.1
GOP: 30 frames, B=2, Ref=4

Color Metadata: Rec.709 SDR

Performance Targets:
- HW Decode FPS: >= 30 fps
- CPU Decode FPS: >= 12 fps (fallback)
- Proxy Decode FPS: >= 60 fps (scale=0.5)
- Seek Time: < 100 ms
- Open Time: < 300 ms
```

---

### 2. H.265 10-bit 4:2:0 HDR

#### h265_10bit_420_4k

```
ID: h265_10bit_420_4k
Name: H.265 10-bit 4:2:0 4K HDR
File: p52/h265_10bit_420_4k.mp4

Resolution: 3840 x 2160
FPS: 24.0
Duration: 10.0s (240 frames)
Bit Depth: 10-bit
Pixel Format: yuv420p10le
Chroma: 4:2:0

Codec: HEVC Main10 Profile Level 5.1
GOP: 48 frames, B=4, Ref=5

Color Metadata:
- Color Space: bt2020nc
- Color Primaries: bt2020
- Color Transfer: smpte2084 (PQ)
- Color Range: tv
- HDR Format: HDR10

Performance Targets:
- HW Decode FPS: >= 24 fps
- CPU Decode FPS: >= 6 fps (fallback, slow)
- Proxy Decode FPS: >= 30 fps (scale=0.25)
- Seek Time: < 150 ms (Long GOP penalty)
- Open Time: < 400 ms
```

---

### 3. ProRes 422 HQ

#### prores_422_hq_1080p

```
ID: prores_422_hq_1080p
Name: ProRes 422 HQ 1080p
File: p52/prores_422_hq_1080p.mov

Resolution: 1920 x 1080
FPS: 24.0
Duration: 10.0s (240 frames)
Bit Depth: 10-bit
Pixel Format: yuv422p10le
Chroma: 4:2:2

Codec: Apple ProRes 422 HQ (profile=3)
GOP: I-frame only (intra-frame)

Color Metadata: Rec.709 SDR

Performance Targets:
- CPU Decode FPS: >= 60 fps (I-frame fast)
- Proxy Decode FPS: >= 120 fps (scale=0.5)
- v3 Full Decode FPS: >= 24 fps (no downgrade)
- Seek Time: < 30 ms (I-frame instant)
- Open Time: < 150 ms

Note: v3 规则 - ProRes 422 HQ 不降级到 proxy
```

#### prores_422_hq_4k

```
ID: prores_422_hq_4k
Name: ProRes 422 HQ 4K
File: p52/prores_422_hq_4k.mov

Resolution: 3840 x 2160
FPS: 24.0
Duration: 10.0s (240 frames)
Bit Depth: 10-bit
Pixel Format: yuv422p10le
Chroma: 4:2:2

Codec: Apple ProRes 422 HQ (profile=3)

Performance Targets:
- CPU Decode FPS: >= 24 fps
- v3 Full Decode FPS: >= 12 fps
- Proxy Decode FPS: >= 48 fps (scale=0.5)
- Seek Time: < 50 ms
- Open Time: < 200 ms
```

---

### 4. ProRes 4444 with Alpha

#### prores_4444_1080p

```
ID: prores_4444_1080p
Name: ProRes 4444 with Alpha 1080p
File: p52/prores_4444_1080p.mov

Resolution: 1920 x 1080
FPS: 24.0
Duration: 5.0s (120 frames)
Bit Depth: 12-bit
Pixel Format: yuva444p12le
Chroma: 4:4:4
Alpha: straight (unassociated)

Codec: Apple ProRes 4444 (profile=4)

Color Metadata: Rec.709 SDR with Alpha

Performance Targets:
- CPU Decode FPS: >= 24 fps (with alpha)
- Proxy Decode FPS: >= 48 fps (scale=0.5)
- Alpha Channel Detected: true
- Seek Time: < 40 ms
- Open Time: < 180 ms

Test Patterns: alpha_checkerboard, gradient_alpha
```

#### prores_4444_4k

```
ID: prores_4444_4k
Name: ProRes 4444 with Alpha 4K
File: p52/prores_4444_4k.mov

Resolution: 3840 x 2160
FPS: 24.0
Duration: 5.0s (120 frames)
Bit Depth: 12-bit
Pixel Format: yuva444p12le
Alpha: straight

Performance Targets:
- CPU Decode FPS: >= 12 fps
- Proxy Decode FPS: >= 24 fps (scale=0.25)
- Alpha Channel Detected: true
- Seek Time: < 80 ms
- Open Time: < 300 ms

Fallback Strategy: High bit depth + alpha triggers proxy fallback
```

---

### 5. DNxHR HQX

#### dnxhr_hqx_4k

```
ID: dnxhr_hqx_4k
Name: DNxHR HQX 4K
File: p52/dnxhr_hqx_4k.mxf

Resolution: 3840 x 2160
FPS: 25.0
Duration: 8.0s (200 frames)
Bit Depth: 10-bit
Pixel Format: yuv422p10le
Chroma: 4:2:2

Codec: Avid DNxHR HQX
Container: MXF
GOP: I-frame only (intra-frame)

Color Metadata: Rec.709 SDR

Performance Targets:
- CPU Decode FPS: >= 30 fps (I-frame fast)
- Proxy Decode FPS: >= 60 fps (scale=0.5)
- Seek Time: < 40 ms
- Open Time: < 180 ms

Note: MXF container requires special handling
```

---

### 6. NV12 Raw Sample

#### nv12_sample_1080p

```
ID: nv12_sample_1080p
Name: NV12 Sample 1080p
File: p52/nv12_sample_1080p.nv12

Resolution: 1920 x 1080
Frame Count: 1
Bit Depth: 8-bit
Pixel Format: nv12 (YUV 4:2:0 semiplanar)
Chroma: 4:2:0

Format Description:
- Y plane: 1920 x 1080 bytes (luminance)
- UV plane: 1920 x 540 bytes (interleaved U/V)
- Total size: 1920 x 1080 + 1920 x 540 = 3,110,400 bytes

Purpose: Hardware decoder output format test
- YUV shader correctness verification
- Chroma interpolation test
- Color range handling test

Shader Tests:
YUV to RGB Conversion (BT.709):
  - Black: Y=16, U=128, V=128 -> RGB(0, 0, 0)
  - White: Y=235, U=128, V=128 -> RGB(255, 255, 255)
  - Red: Y=82, U=90, V=240 -> RGB(255, 0, 0)
  - Green: Y=144, U=54, V=34 -> RGB(0, 255, 0)
  - Blue: Y=41, U=240, V=110 -> RGB(0, 0, 255)
  Tolerance: 2 pixels

Chroma Interpolation:
  - Half-pixel offset handling
  - Visual inspection of interpolated edges

Color Range Handling:
  - TV Range (16-235) vs Full Range (0-255)
```

---

### 7. P010 Raw Sample

#### p010_sample_1080p

```
ID: p010_sample_1080p
Name: P010 Sample 1080p
File: p52/p010_sample_1080p.p010

Resolution: 1920 x 1080
Frame Count: 1
Bit Depth: 10-bit
Pixel Format: p010le (10-bit NV12)
Chroma: 4:2:0

Format Description:
- Y plane: 1920 x 1080 x 2 bytes (16-bit per Y, effective 10-bit)
- UV plane: 1920 x 540 x 2 bytes (interleaved U/V, 16-bit)
- Total size: 1920 x 1080 x 2 + 1920 x 540 x 2 = 6,220,800 bytes

Purpose: HDR hardware decoder output format test
- 10-bit YUV shader correctness
- PQ transfer function test
- BT.2020 gamut mapping test

Shader Tests:
YUV to RGB Conversion (10-bit BT.709):
  - Black: Y=64, U=512, V=512 -> RGB(0, 0, 0)
  - White: Y=940, U=512, V=512 -> RGB(1023, 1023, 1023)
  - Mid-gray: Y=512, U=512, V=512 -> RGB(512, 512, 512)
  Tolerance: 4 values

PQ Transfer Function (EOTF):
  - Input 0.0 -> Output 0.0 (black)
  - Input 0.01 -> Output ~0.08 (near black)
  - Input 0.5 -> Output ~0.52 (mid)
  - Input 1.0 -> Output 1.0 (peak)
  Tolerance: 0.01

BT.2020 to BT.709 Gamut Mapping:
  - Gamut boundary check
  - Out-of-gamut handling
```

---

## 素材组定义

### p52_standard_matrix

完整 P5-2 测试矩阵：
```
h264_8bit_420_1080p
h264_8bit_420_4k
h265_10bit_420_4k
prores_422_hq_1080p
prores_422_hq_4k
prores_4444_1080p
prores_4444_4k
dnxhr_hqx_4k
nv12_sample_1080p
p010_sample_1080p
```

### p52_hw_accel

硬件加速场景测试：
```
h264_8bit_420_1080p  (1080p H.264 硬解)
h264_8bit_420_4k     (4K H.264 硬解)
h265_10bit_420_4k    (4K H.265 HDR 硬解)
```

### p52_proxy

Proxy 播放场景测试：
```
h264_8bit_420_1080p  (H.264 proxy)
h264_8bit_420_4k     (H.264 4K proxy)
prores_422_hq_1080p  (ProRes proxy, v3 不降)
prores_422_hq_4k     (ProRes 4K proxy)
```

### p52_alpha

Alpha 通道处理测试：
```
prores_4444_1080p  (1080p alpha)
prores_4444_4k     (4K alpha)
```

### p52_intra

I-frame 编码测试：
```
prores_422_hq_1080p  (ProRes intra)
prores_422_hq_4k     (ProRes 4K intra)
dnxhr_hqx_4k         (DNxHR intra)
```

### p52_shader_test

YUV Shader 正确性测试：
```
nv12_sample_1080p  (8-bit YUV shader)
p010_sample_1080p  (10-bit HDR shader)
```

---

## 测试场景定义

### 1. 硬件加速场景 (hw_acceleration)

测试硬件解码器可用性和性能。

**要求：**
- 检测硬件解码器可用性
- 验证硬件解码帧率达标
- 验证 fallback 到 CPU 解码路径

**素材：**
- h264_8bit_420_1080p
- h264_8bit_420_4k
- h265_10bit_420_4k

---

### 2. Proxy 播放场景 (proxy_playback)

测试降分辨率解码的性能和正确性。

**要求：**
- 验证 Proxy 解码帧率达标
- 验证 Proxy 缩放因子正确
- 验证 v3 不降级规则

**素材：**
- h264_8bit_420_1080p
- h264_8bit_420_4k
- prores_422_hq_1080p
- prores_422_hq_4k

---

### 3. Alpha 通道处理 (alpha_handling)

测试 Alpha 通道检测、解码和混合的正确性。

**要求：**
- 检测 Alpha 通道存在
- 验证 Alpha 混合正确性
- 验证高位深 fallback 策略

**素材：**
- prores_4444_1080p
- prores_4444_4k

---

### 4. I-frame 编码测试 (intra_codec)

测试 I-frame only 编码的解码和 seek 性能。

**要求：**
- 验证 seek 性能（I-frame only 应快）
- 验证解码帧率

**素材：**
- prores_422_hq_1080p
- prores_422_hq_4k
- dnxhr_hqx_4k

---

### 5. YUV Shader 正确性 (yuv_shader_correctness)

测试 YUV 到 RGB 转换 shader 的正确性。

**要求：**
- YUV to RGB 转换正确性
- 色度插值正确性
- 色彩范围处理正确性
- HDR 传输函数正确性

**素材：**
- nv12_sample_1080p
- p010_sample_1080p

---

## 素材生成与下载

### 使用 generate_test_fixtures.py

```bash
# 列出所有可用素材
python tests/media/generate_test_fixtures.py --list

# 生成所有素材
python tests/media/generate_test_fixtures.py --all

# 生成特定素材
python tests/media/generate_test_fixtures.py --fixtures h264_8bit_420_1080p,prores_422_hq_1080p

# 生成特定组
python tests/media/generate_test_fixtures.py --group p52_hw_accel

# 强制重新生成
python tests/media/generate_test_fixtures.py --all --force
```

### 使用 download_test_fixtures.py

```bash
# 列出所有素材组
python tests/media/download_test_fixtures.py --list

# 下载或生成所有素材
python tests/media/download_test_fixtures.py --all

# 下载特定组
python tests/media/download_test_fixtures.py --group p52_intra

# 验证现有素材
python tests/media/download_test_fixtures.py --verify
```

---

## 性能基准测试

### 使用 benchmark_video_pipeline.py

```bash
# 对特定素材运行基准测试
python tests/media/benchmark_video_pipeline.py --fixture h264_8bit_420_1080p

# 对所有 P5-2 素材运行基准测试
python tests/media/benchmark_video_pipeline.py --group p52_standard_matrix

# 输出 JSON 格式结果
python tests/media/benchmark_video_pipeline.py --all --output json > results.json
```

### 使用 performance_validation.py

```bash
# 验证性能目标
python tests/media/performance_validation.py --benchmark-results results.json

# 运行 pytest 验证
pytest tests/media/performance_validation.py -v
```

---

## 文件位置

```
tests/media/
  fixtures_manifest.json      # 素材定义清单
  performance_targets.json    # 性能目标定义
  generate_test_fixtures.py   # 素材生成脚本
  download_test_fixtures.py   # 素材下载脚本
  benchmark_video_pipeline.py # 基准测试脚本
  performance_validation.py   # 性能验证脚本
  synthetic_fixture_generator.py # 合成素材生成器（原有）

tests/media/fixtures/
  p52/
    h264_8bit_420_1080p.mp4
    h264_8bit_420_4k.mp4
    h265_10bit_420_4k.mp4
    prores_422_hq_1080p.mov
    prores_422_hq_4k.mov
    prores_4444_1080p.mov
    prores_4444_4k.mov
    dnxhr_hqx_4k.mxf
    nv12_sample_1080p.nv12
    p010_sample_1080p.p010
```

---

---

## W0-01 Windows 解码素材矩阵（2026-09-12 增补）

计划 `docs/windows_optimization_plan_2026-09-11.md` W0-01：Windows 解码素材清单，
在 P5-2 矩阵之上扩展 AV1 与 4:2:2 诊断用例（**4:2:2 10-bit 必含**）。

| ID | 编码 | 色深 | 色度 | 分辨率 | 硬解期望 |
|---|---|---|---|---|---|
| av1_8bit_420_1080p | AV1 (libaom-av1) | 8-bit | 4:2:0 | 1080p | vendor_dependent（RDNA2+/Ampere+/DG+） |
| av1_10bit_420_4k | AV1 | 10-bit | 4:2:0 | 4K | vendor_dependent |
| h264_422_8bit_1080p | H.264 High 4:2:2 | 8-bit | 4:2:2 | 1080p | unsupported_diag |
| h264_422_10bit_1080p | H.264 High 4:2:2 | 10-bit | 4:2:2 | 1080p | **unsupported_diag（必含）** |
| h265_422_10bit_1080p | HEVC Main 4:2:2 10 | 10-bit | 4:2:2 | 1080p | unsupported_diag |

诊断用例语义：NVDEC/D3D11VA 均不支持 4:2:2；硬解 session 可能成功打开但
PyAV 静默回退软解（输出 yuv422p10le）——`WindowsHwPlanarSource.open()`
首帧验证必须以 `MediaOpenError` + `codec_unsupported_by_hw:<codec>/<fmt>`
显式拒绝，回退 CPU planar。**禁止静默软解伪装硬解**（计划 §4 硬规则）。

生成与分组：

```bash
python tests/media/generate_test_fixtures.py --group win_hw_decode_matrix
python tests/media/generate_test_fixtures.py --group win_hw_decode_diag   # 仅诊断用例
```

规格元数据见 `fixtures_manifest.json` 的 `windows_hw_decode_matrix` 节
（含 `hw_decode_expectation` 三值语义：required / vendor_dependent /
unsupported_diag）。真实拍摄素材（H.264/HEVC/ProRes/DNxHR，2K/4K）另见
`tests/test_videos/`。

## 版本历史

- 2026-05-21: P5-2 标准素材矩阵初始版本
- 2026-09-12: W0-01 增补 Windows 解码素材矩阵（AV1 + 4:2:2 诊断用例）
