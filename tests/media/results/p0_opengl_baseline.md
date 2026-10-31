# P0-04 OpenGL 基线快照（含 4K 档）

> 生成日期：2026-09-11　git `7c147ac97c9e`（chore/edit 分支）
> 工具：`tests/benchmark/benchmark_yuv_gpu_pipeline.py`（P0-02 升级版：git_sha + 环境信息 + 四维 render_path + wall 墙钟地板）
> 机器：MacPro7,1 (Hackintosh) / i9-9900K / AMD Radeon RX 6800 (OpenGL Engine) / OpenGL 4.1 ATI-6.1.13 / macOS 15.3.1

## 四维渲染路径（所有归档统一字段）

```text
graphics_api = opengl_4.1          compute_api = glsl_fragment
decode_api   = cpu_ffmpeg_sw       interop_api = none_numpy_handoff
```

## 归档

| 归档 | 素材 | 性质 |
|---|---|---|
| `p0_opengl_baseline_real2k_prores.json` | `tests/test_videos/2K_prores422HQ.mov`（1920×1080 yuv422p10le 25fps，647 帧） | **真实素材** |
| `p0_opengl_baseline_synthetic4k_prores.json` | `tests/media/fixtures/p52/prores_422_hq_4k.mov`（3840×2160 yuv422p10le 24fps，240 帧） | **合成素材（testsrc2）** |

## 结果（120 帧 + 20 warmup，r16ui_direct 路径，GPU/R16UI self-test 全部 PASSED）

| 档 | wall FPS（地板） | 归因 total avg | texture_write avg | render avg | 备注 |
|---|---:|---:|---:|---:|---|
| 真实 2K 1920×1080（原生档） | **235.8** | 2.69 ms | 2.3 ms | 0.1 ms | 解码线程与 GPU 重叠 |
| 真实 2K 1280×720（proxy 档） | 100.9 | 6.84 ms | 0.5 ms | 0.2 ms | 归因 decode 5.9ms 主要是 swscale 重采样 |
| 合成 4K 3840×2160（全分辨率档） | **63.4** | 10.92 ms | 10.3 ms | 0.3 ms | write = 66MB/帧 ≈ 6.4 GB/s 上传 |

## 测量语义勘误（本次实测发现，比数字更重要）

1. **线程化解码的逐帧归因是近似的。** `TOPOS_VIDEO thread_type=AUTO` 下 `packet.decode()`
   只等当前包，重活在 worker 线程与后续 GPU 工作重叠——4K 档归因 decode 仅 0.1ms，
   但 wall 地板显示每帧还有 ~4.9ms 未归因（解码尾巴）。**跨后端对比一律以
   `wall_effective_fps` 为准**；分项数字只用于相对变化（同一素材同一机器前后对比）。
2. **像素物化成本**：PyAV 帧 `to_ndarray()` 会额外付出 ~30ms/帧（4K）的全量拷贝；
   本 harness 用 `plane_to_array`（零拷贝视图 + 64 字节对齐时免拷贝）避开，
   与真实播放路径的零拷贝语义一致。
3. **plane_prepare ≈ 0 的历史结论在本基线复现成立**（~0.001ms 量级，三档全部）。

## 4K 档现状与限制（诚实声明）

- **合成 4K ≠ 真实 4K**：testsrc2 内容熵极低（156MB / 240 帧 ≈ 650KB/帧，真实 ProRes 4K
  约 5MB/帧），解码快、零拷贝评估价值有限。合成 4K 档只用于**管线正确性与
  上传/渲染分段**的基线，**不得用于解码性能门禁**（P0-03 阻塞项，见下）。
- 4K 档 OpenGL 基线的**解码上限**数据缺失，等真实 4K 素材到位后补测
  （对应计划 §13 开放问题 #8）。

## P0-03 基准素材来源状态

| 项 | 状态 |
|---|---|
| `fixtures_manifest.json` 声明的 4K/ProRes 真实素材 | **本机不存在**，`download_base_url` 为占位符 `https://example.com/topos-fixtures` |
| 合成 4K（H.264 8bit 420 + ProRes 422 HQ 10bit 422） | ✅ 已生成 `tests/media/fixtures/p52/`（已 gitignore） |
| 真实 2K ProRes / H.264 / HEVC / DNxHR / topos | ✅ `tests/test_videos/`（gitignore，本机已有） |
| 真实 4K H.264/HEVC/ProRes | ❌ **阻塞：需素材托管决策**（自建下载源或指定商用素材库） |

**生成器 bug 修复记录**：`generate_test_fixtures.py` 的 `-t 10.0` 曾放在第一个
lavfi 输入之后，成了第二个（音频）输入的时长限制，无限 testsrc2 视频源永不停止
（曾产出 59GB 失控文件 + 300s 必然超时）。已改为输出选项并在超时路径补
`unlink`。
