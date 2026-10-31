# 视频播放性能基准测试报告

**日期**: 2026-06-07  
**测试目的**: 对比三种基准测试的帧率差异，定位视频播放卡顿的根本原因

---

## 1. 测试概述

### 1.1 测试脚本

| 脚本 | 测试层级 | 测试内容 | 涉及 GPU |
|-----|---------|---------|---------|
| `benchmark_decode_speed.py` | 底层解码 | 纯解码速度（无渲染） | ❌ |
| `benchmark_timeline_playback.py` | 中层服务 | 服务层 + 缓存 + CPU 合成 | ❌ |
| `benchmark_viewport_playback.py` | 完整渲染 | Decode → GPU Upload → Shader → Composite | ✅ |

### 1.2 测试视频

| 视频 | 分辨率 | 编码 | 大小 |
|-----|--------|------|------|
| 2K_h264.mp4 | 1920×1080 | H.264 | 52 MB |
| 2K_H265.mov | 1920×1080 | H.265/HEVC | 32 MB |
| 2K_prores422HQ.mov | 1920×1080 | ProRes 422 HQ | 519 MB |
| 2K_DNxHR HQX 12-bit.mov | 1920×1080 | DNxHR HQX | 690 MB |
| 4K_H264.mov | 3840×2160 | H.264 | 54 MB |
| 4K_H265.mov | 3840×2160 | H.265/HEVC | 38 MB |
| 4K_prores422HQ.mov | 3840×2160 | ProRes 422 HQ | 2.6 GB |
| 4K_DNxHR HQX 12-bit.mov | 3840×2160 | DNxHR HQX | 2.7 GB |

### 1.3 测试参数

| 参数 | decode_speed | timeline_playback | viewport_playback |
|-----|--------------|-------------------|-------------------|
| 预热帧数 | 5 | 3-5 | 5 |
| 测试帧数 | 100 | 60 | 60 |
| 视口大小 | 无 | 无 | 1920×1080 |

---

## 2. 测试结果对比

### 2.1 单视频播放帧率（核心对比）

#### 2K 视频对比

| 测试脚本 | 2K_h264 | 2K_H265 | 2K_prores | 2K_DNxHR |
|---------|---------|---------|-----------|----------|
| **decode_speed** (DecodeManager seq) | **1136 fps** | ~485 fps | **579 fps** | ~862 fps |
| **timeline_playback** (GPU Decode) | **617 fps** | 276 fps | - | - |
| **viewport_playback** (完整渲染) | **32 fps** | **33 fps** | **24 fps** | **24 fps** |

#### 关键发现：帧率差异高达 35 倍！

```
decode_speed      1136 fps  ← 只测解码
viewport_playback    32 fps  ← 完整渲染管线

差异：1136 / 32 = 35 倍
```

#### 4K 视频对比

| 测试脚本 | 4K_H264 | 4K_H265 | 4K_prores | 4K_DNxHR |
|---------|---------|---------|-----------|----------|
| **decode_speed** (Planar) | 369 fps | 133 fps | 113 fps | 205 fps |
| **viewport_playback** (完整渲染) | **38 fps** | **7 fps** | **6 fps** | **6 fps** |

---

## 3. 瓶颈定位分析

### 3.1 viewport_playback 逐环节耗时分解

#### 2K 视频（GPU upload 是瓶颈）

| 视频 | decode | upload | composite | 总计 | 瓶颈占比 |
|-----|--------|--------|-----------|------|---------|
| 2K_h264 | 0.7ms | **30.1ms** | 0.1ms | 31ms | upload 97% |
| 2K_H265 | 1.0ms | **29.6ms** | 0.0ms | 31ms | upload 97% |
| 2K_prores | 1.6ms | **40.5ms** | 0.0ms | 42ms | upload 96% |
| 2K_DNxHR | 1.4ms | **39.7ms** | 0.0ms | 41ms | upload 97% |

#### 4K 视频（decode + upload 双瓶颈）

| 视频 | decode | upload | composite | 总计 | 主要瓶颈 |
|-----|--------|--------|-----------|------|---------|
| 4K_H264 | **21.7ms** | 4.6ms | 0.0ms | 26ms | decode 82% |
| 4K_H265 | **54.5ms** | **87.6ms** | 0.1ms | 142ms | decode + upload |
| 4K_prores | 4.6ms | **163.3ms** | 0.0ms | 168ms | upload 97% |
| 4K_DNxHR | 3.6ms | **161.2ms** | 0.0ms | 165ms | upload 97% |

### 3.2 理论预期 vs 实际结果

| 场景 | 理论预期 | 实际结果 | 原因 |
|-----|---------|---------|------|
| **2K 单视频** | decode_speed ≈ viewport | ❌ **35 倍差距** | GPU upload 占用 97% 时间 |
| **4K H264** | decode 成瓶颈 | ✅ decode 82% | 4K 解码确实更慢 |
| **4K ProRes/DNxHR** | 解码快，渲染快 | ❌ 只有 6 fps | GPU upload 处理 4K 数据耗时巨大 |

---

## 4. 根本原因分析

### 4.1 🔑 核心发现：GPU 上传是真正的瓶颈

```
                    decode_speed 测试
                    ┌─────────────────────────────┐
                    │  Decode Only: 1136 fps      │
                    │  (0.9ms per frame)          │
                    └─────────────────────────────┘
                                 ↓
                    viewport_playback 测试
                    ┌─────────────────────────────┐
                    │  Decode:     0.7ms (2%)     │
                    │  GPU Upload: 30ms  (97%) ← 瓶颈！│
                    │  Composite:  0.1ms (1%)     │
                    │  Total:      31ms → 32 fps │
                    └─────────────────────────────┘
```

### 4.2 为什么 GPU 上传如此慢？

根据测试数据分析，可能原因：

1. **YUV 平面上传无异步机制**
   - 每帧上传 Y/U/V 三个纹理
   - `texture.write()` 可能触发 GPU 同步等待
   - 缺少 PBO (Pixel Buffer Object) 异步上传

2. **纹理池复用问题**
   - 每帧可能创建新纹理而不是复用
   - 纹理创建/释放开销大

3. **数据拷贝过多**
   - Planar YUV → numpy array → texture.write
   - 可能存在多次内存拷贝

4. **ModernGL standalone context 限制**
   - 测试使用 standalone context，可能缺少某些优化

### 4.3 对比开源项目

| 项目 | GPU 上传方式 | 性能 |
|-----|-------------|------|
| **kdenlive** | Threaded OpenGL + Metal | 高效 |
| **Natron** | 纯 CPU 渲染 | 无 GPU 上传瓶颈 |
| **Topos Color** | ModernGL texture.write | **慢（30ms/帧）** |

kdenlive 在 `openglvideowidget.cpp:334-355` 使用 Threaded OpenGL 检测：
```cpp
// Threaded OpenGL detection
if (glGetIntegerv && mlt_properties_get(properties, "glsl")) {
    // Use threaded upload for better performance
}
```

---

## 5. 结论与建议

### 5.1 结论

| 结论 | 说明 |
|-----|------|
| **解码不是瓶颈** | decode_speed 测试显示解码速度足够快（100-1100 fps） |
| **GPU 上传是瓶颈** | viewport_playback 显示 GPU upload 占 97% 时间（30ms/帧） |
| **三个测试不一致** | decode_speed 和 viewport_playback 有 35 倍差距 |

### 5.2 优化建议（按优先级）

#### 🔥 高优先级：优化 GPU 上传

1. **使用 PBO (Pixel Buffer Object) 异步上传**
   ```python
   # 当前方式（同步）
   texture.write(data)  # 等待 GPU 完成写入
   
   # 建议方式（异步）
   pbo = ctx.buffer(data)  # 创建 PBO
   texture.write(pbo)      # 异步上传
   ```

2. **纹理池复用**
   - 确保同一帧索引复用相同纹理
   - 避免 `texture.write()` 后立即释放

3. **减少数据拷贝**
   - 直接传递 FFmpeg 解码的 buffer 到 GPU
   - 避免 numpy array 中间转换

#### 🟡 中优先级：解码路径优化

4. **4K H265 硬解码优化**
   - 当前 fallback 到 CPU decode（54.5ms）
   - 需修复 `p010le` 格式支持

5. **VideoToolbox 零拷贝**
   - 当前 VT 解码后下载到 CPU
   - 应实现 IOSurface → Metal/OpenGL 直接传递

#### 🟢 低优先级：渲染优化

6. **Shader 优化**
   - 当前 composite 只有 0.1ms，已经够快
   - YUV→RGB shader 也可优化，但不是瓶颈

---

## 6. 详细测试数据

### 6.1 benchmark_decode_speed.py 完整结果

| 视频 | CPU SW (packed) | Planar GPU scale | Planar CPU reformat | DecodeManager seq | DecodeManager cached | DecodeManager seek |
|-----|-----------------|------------------|---------------------|-------------------|---------------------|-------------------|
| 2K_h264 | 368 fps | 1095 fps | 1056 fps | **1136 fps** | 1120 fps | 28 fps |
| 2K_prores | 142 fps | 579 fps | 583 fps | **579 fps** | 587 fps | 31 fps |
| 2K_H265 | 129 fps | 485 fps | 502 fps | ~485 fps | - | - |
| 2K_DNxHR | - | 862 fps | 837 fps | **862 fps** | - | - |
| 4K_H264 | - | 369 fps | 82 fps | - | - | - |
| 4K_H265 | - | 133 fps | 56 fps | - | - | - |
| 4K_prores | - | 113 fps | 45 fps | - | - | - |
| 4K_DNxHR | - | 205 fps | 40 fps | - | - |

### 6.2 benchmark_viewport_playback.py 完整结果

#### 单轨道 (Single Track)

| 视频 | FPS | Avg (ms) | P95 (ms) | decode (ms) | upload (ms) | composite (ms) | 状态 |
|-----|-----|----------|----------|-------------|-------------|----------------|------|
| 2K_h264 | 32.4 | 30.9 | 32.3 | 0.7 | **30.1** | 0.1 | ✅ ≥24fps |
| 2K_H265 | 32.7 | 30.6 | 30.9 | 1.0 | **29.6** | 0.0 | ✅ ≥24fps |
| 2K_prores | 23.7 | 42.1 | 43.4 | 1.6 | **40.5** | 0.0 | ⚠️ <24fps |
| 2K_DNxHR | 24.3 | 41.1 | 42.3 | 1.4 | **39.7** | 0.0 | ✅ ≥24fps |
| 4K_H264 | 37.9 | 26.4 | 36.7 | **21.7** | 4.6 | 0.0 | ✅ ≥24fps |
| 4K_H265 | 7.0 | 142.1 | 157.9 | **54.5** | **87.6** | 0.1 | ❌ <12fps |
| 4K_prores | 6.0 | 167.9 | 168.6 | 4.6 | **163.3** | 0.0 | ❌ <12fps |
| 4K_DNxHR | 6.1 | 164.8 | 168.9 | 3.6 | **161.2** | 0.0 | ❌ <12fps |

#### 双轨道 (Dual Track)

| 视频 | FPS | decode (ms) | upload (ms) | 状态 |
|-----|-----|-------------|-------------|------|
| 2K_h264 + 2K_h264 | 10.6 | 33.2 | 60.7 | ❌ |
| 2K_h264 + 2K_DNxHR | 14.2 | 1.3 | 68.9 | ⚠️ |
| 4K_H264 + 4K_H264 | 2.6 | 381.9 | 9.3 | ❌ |
| 4K_H264 + 4K_DNxHR | 5.3 | 25.0 | 163.4 | ❌ |

---

## 7. 附录：测试执行命令
测试素材位置：/Users/testuser/Documents/vscode/topos-color-multitask\ /topos-color01/tests/test_videos
```bash
# 解码速度测试
.venv/bin/python tests/benchmark/benchmark_decode_speed.py

# 时间线播放测试
.venv/bin/python tests/benchmark/benchmark_timeline_playback.py

# 视口渲染测试
.venv/bin/python tests/benchmark/benchmark_viewport_playback.py
```

---

## 8. 总结

### 真正导致播放卡顿的原因

**不是解码慢，而是 GPU 上传慢！**

| 环节 | 时间占比 | 状态 |
|-----|---------|------|
| 解码 | 0.7-1.6ms (2-4%) | ✅ 已优化 |
| **GPU 上传** | **30-40ms (97%)** | ❌ **瓶颈** |
| 合成 | 0.1ms (1%) | ✅ 已优化 |

### 下一步行动

1. 检查 `src/shared/video/yuv_gpu_upload.py` 的实现
2. 研究 ModernGL PBO 异步上传
3. 对比 kdenlive Threaded OpenGL 实现
4. 实现 IOSurface → Metal 零拷贝（macOS）

---

**报告生成**: 2026-06-07  
**测试环境**: macOS, Python 3.x, ModernGL standalone context