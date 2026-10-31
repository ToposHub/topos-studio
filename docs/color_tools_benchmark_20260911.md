# 调色页调色工具性能基准（2026-09-11）

对应 `docs/metal_backend_migration_and_cross_platform_gpu_plan.md` §0.3.1。
原始 JSON：`tests/media/results/color_tools_20260911.json`

## 测量口径

- 工具：`tools/bench_color_tools.py`（项目自带，未修改）
- 素材：`tests/test_videos/2K_h264.mp4`、`tests/test_videos/4K_H264.mov`（真实素材）
- 管线：真实 Color 页 `AsyncRenderer` GPU 链路，`video_render_sync_mode='finish'`
  （每帧强制 GPU 排空，因此耗时含 GPU 时间）
- 帧数：预解码 96 帧，每场景测量 96 帧，warmup 10
- 解码单独报告，不占渲染预算

## 结果

### 2K 1920x1080
- 解码上限（单独报告）: 227.5 fps (4.40 ms/frame)
- baseline（恒等调色 = 上传+present+sync 地板）: 1.74 ms/frame

| 场景 | ms_mean | ms_p50 | ms_p95 | Δ vs baseline |
|---|---:|---:|---:|---:|
| `combo_full` | 3.85 | 3.82 | 4.15 | +2.11 |
| `combo_full_pb` | 3.84 | 3.82 | 4.12 | +2.10 |
| `plugin_skin` | 2.80 | 2.79 | 3.02 | +1.06 |
| `qualifier_fuzz` | 2.69 | 2.70 | 2.87 | +0.95 |
| `layers_3` | 2.62 | 2.61 | 2.85 | +0.88 |
| `usm_heavy` | 2.52 | 2.51 | 2.73 | +0.78 |
| `layers_2` | 2.23 | 2.21 | 2.44 | +0.49 |
| `plugin_hpf` | 2.17 | 2.15 | 2.34 | +0.43 |
| `blur_optical` | 2.10 | 2.09 | 2.24 | +0.36 |
| `qualifier_reframe` | 2.09 | 2.08 | 2.26 | +0.35 |
| `mask_reframe` | 2.04 | 2.02 | 2.23 | +0.30 |
| `mask_qualifier` | 2.01 | 1.97 | 2.22 | +0.26 |
| `primary_drag` | 2.00 | 1.98 | 2.15 | +0.26 |
| `blur_soften_r30` | 1.99 | 1.98 | 2.13 | +0.25 |
| `blur_soften` | 1.97 | 1.96 | 2.13 | +0.23 |
| `blur_motion` | 1.94 | 1.93 | 2.10 | +0.20 |
| `combo_typical` | 1.94 | 1.90 | 2.14 | +0.20 |
| `grain` | 1.89 | 1.87 | 2.08 | +0.15 |
| `mask_circle` | 1.89 | 1.88 | 2.04 | +0.15 |
| `sharpen_usm` | 1.86 | 1.84 | 1.99 | +0.12 |
| `blur_fast` | 1.82 | 1.80 | 1.98 | +0.08 |
| `qualifier` | 1.98 | 1.97 | 2.15 | +0.24 |
| `softclip` | 1.79 | 1.76 | 1.94 | +0.05 |
| `lut3d` | 1.79 | 1.78 | 1.95 | +0.05 |
| `detail` | 1.76 | 1.74 | 1.90 | +0.02 |
| `primary` | 1.76 | 1.74 | 1.91 | +0.02 |
| `mixer` | 1.75 | 1.73 | 1.89 | +0.01 |
| `hsl_curves` | 1.75 | 1.74 | 1.90 | +0.01 |
| `curves` | 1.75 | 1.73 | 1.89 | +0.01 |
| `warper` | 1.75 | 1.73 | 1.88 | +0.01 |
| `baseline` | 1.74 | 1.71 | 1.94 | +0.00 |

### 4K 3840x2160
- 解码上限（单独报告）: 55.3 fps (18.09 ms/frame)
- baseline（恒等调色 = 上传+present+sync 地板）: 5.46 ms/frame

| 场景 | ms_mean | ms_p50 | ms_p95 | Δ vs baseline |
|---|---:|---:|---:|---:|
| `combo_full` | 12.37 | 12.36 | 12.75 | +6.91 |
| `combo_full_pb` | 12.38 | 12.33 | 12.83 | +6.91 |
| `usm_heavy` | 10.87 | 10.89 | 11.16 | +5.41 |
| `plugin_skin` | 9.55 | 9.52 | 9.96 | +4.09 |
| `qualifier_fuzz` | 8.39 | 8.34 | 8.83 | +2.93 |
| `plugin_hpf` | 8.24 | 8.21 | 8.51 | +2.77 |
| `blur_optical` | 7.59 | 7.59 | 8.00 | +2.13 |
| `layers_3` | 7.01 | 6.96 | 7.45 | +1.54 |
| `qualifier_reframe` | 6.50 | 6.50 | 6.99 | +1.03 |
| `mask_reframe` | 6.29 | 6.35 | 6.83 | +0.83 |
| `primary_drag` | 6.16 | 6.29 | 6.70 | +0.70 |
| `layers_2` | 6.09 | 6.06 | 6.49 | +0.62 |
| `blur_soften_r30` | 5.99 | 5.78 | 6.64 | +0.53 |
| `blur_soften` | 5.96 | 5.77 | 6.64 | +0.50 |
| `blur_motion` | 5.88 | 5.67 | 6.61 | +0.42 |
| `mask_qualifier` | 5.74 | 5.60 | 6.57 | +0.28 |
| `qualifier` | 5.71 | 5.55 | 6.52 | +0.25 |
| `grain` | 5.54 | 5.52 | 5.96 | +0.08 |
| `mask_circle` | 5.52 | 5.44 | 6.30 | +0.06 |
| `primary` | 5.47 | 5.37 | 6.33 | +0.01 |
| `sharpen_usm` | 5.45 | 5.39 | 6.34 | -0.01 |
| `blur_fast` | 5.45 | 5.32 | 6.33 | -0.01 |
| `combo_typical` | 5.45 | 5.37 | 5.90 | -0.01 |
| `detail` | 5.45 | 5.36 | 5.95 | -0.01 |
| `curves` | 5.38 | 5.35 | 5.80 | -0.08 |
| `hsl_curves` | 5.37 | 5.34 | 5.80 | -0.09 |
| `mixer` | 5.35 | 5.30 | 5.74 | -0.11 |
| `lut3d` | 5.21 | 5.14 | 5.64 | -0.25 |
| `warper` | 5.19 | 5.16 | 5.61 | -0.27 |
| `softclip` | 5.17 | 5.10 | 5.61 | -0.29 |
| `baseline` | 5.46 | 5.35 | 6.36 | +0.00 |

## 结论

1. **调色成本随像素数近似线性**：Δ vs baseline 的 4K/2K 比 = 3.3×（接近 4× 的像素比）。
2. **4K 单层全工具调色 +6.91 ms**，相当于同分辨率解码 18.09 ms 的 **38%**。
   不要把它当成"<1%"——那个数字来自 1080p 播放路径 + 近似恒等调色，不可外推。
3. **最大单项是 5.46 ms 的 baseline 地板**（上传 + present + sync），与调色 delta 同量级。
   地板与 shader 成本必须分别归因：Metal 对 present 段有把握，上传段要靠零拷贝。
4. **4K 解码 18.09 ms 是硬地板**：即使调色降到 0，4K 播放上限仍是 55 fps。
   所以调色后端迁移的收益应计在「交互延迟 / 多层 / 导出」，不是播放帧率。
5. **最贵场景**（`combo_full` / `usm_heavy` / `plugin_skin` / `qualifier_fuzz` / `blur_optical`）
   正是 compute shader + 共享内存最擅长的负载，而 macOS OpenGL 4.1 完全没有 compute。

## 本基准未覆盖

- **Metal 与 macOS OpenGL 在同一 shader 上的 A/B**——决定 Phase 6 是否值得的问题（计划 P1-05）。
- **Composite 页（节点图）** 的成本结构。本表是 Color 页 `AsyncRenderer`；
  Composite 走 `node_engine` + `compute_shader_manager`，无对等基准数据，
  而 macOS 的 compute 能力缺口正落在那里。
- **4K 导出**（非预览）路径。
