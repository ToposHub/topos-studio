# 合成页全节点性能审计报告（真实素材，2026-10-14）

> 素材：`tests/test_videos/2K_prores422HQ.mov`、`4K_prores422LT.mov`（ffmpeg 提取真实帧：3×4K / 5×2K）
> 驱动：`scripts/bench_all_composite_nodes_real_topos.py`（99 个注册节点全量，经生产链路 `CompositeFrameRenderer.render_comp_clip_frame`）+ `scripts/bench_cp_export_real_media.py`（导出速度）
> 环境：macOS GL 4.1 / AMD RX 6900 XT（headless 无 compute → 走文档化的 fragment 回退，即本机生产路径）
> 数据：`bench_out/extended_real_tests_20260917/real_topos_all_composite_nodes.json`（99 节点逐项）、`bench_out/cp_audit_20261014/export_speed.json`

## 一、逐节点基准结论（1080p 真实帧）

**状态分布：81 通过（非平输出）/ 4 缺 AI 模型 / 13 缺 3D 资源（源节点需场景接线，by-design）/ 1 flat（mask_merge，fixture 问题，见 §四）**

| 指标 | 数值 |
|---|---|
| 中位数 | preview 6.0ms / export 59.1ms |
| 最快 | shuffle 4.6ms（zblur/retime/normalize ≈4.9ms） |
| GPU 类普遍区间 | preview 4.6–9.4ms |
| 最慢（正常类） | light_wrap 11.0ms、merge/blend ≈10ms（双输入 + 擦除边） |
| 唯一离群 | **deflicker preview 77ms / export 122ms**（时间域算法，见 §三） |

**CP 批次新增 6 节点全部健康**：grain 3.6 / degrain 3.6 / vectorblur 3.4 / colorlookup 3.8 / cdl 5.1 / keymix 5.0 ms（preview），与 GPU 类基线一致。

**4K 抽查**（blur/glow/keyer/light_wrap）：preview 9.2–9.4ms —— 4 像素量耗时仅 ~1.7×（亚线性，GPU 利用充分）；export ~300ms 为 4K linear_f32 读回 + 编码的管线底噪，非效果成本。

## 二、导出速度（1080p，5–10 帧）

| 场景 | 耗时 | 结论 |
|---|---|---|
| baseline PNG 序列 | 3.8 fps | 管线底噪（解码+求值+读回+编码） |
| **heavy 六连效果链**（blur24+grade+glow+degrain+colorlookup+cdl）PNG | 3.9 fps | **效果链相对底噪 ≈ 零成本** |
| baseline EXR / heavy EXR | 4.3 / 3.8 fps | 同上 |
| heavy 链视频 H.264 CRF23（并行 2 帧） | 4.3 fps | 修复 NameError 后恢复（§四-1） |
| EXR 写出器：OpenEXR 绑定（单 part PIZ） | **213 ms/帧** | **比 cv2 快 7×** |
| EXR 写出器：绑定（beauty+3 AOV 多 part） | 345 ms/帧 | 多 part 仍快于 cv2 单 part |
| EXR 写出器：cv2 单 part（pxr24） | 1482 ms/帧 | 慢路径 |

**建议**（挂账，不在本次实施）：单层 EXR 默认档（"default"）当前映射 cv2 pxr24；绑定可用时改路由 PIZ 可把 EXR 序列导出提速约 7×（CP-006 已为显式 zip/zips/piz/pxr24/dwaa/dwab 档做了绑定路由）。

## 三、GPU 有效性评估

- **GPU 类节点全部符合预期**：单 pass/多 pass fragment 路径，无隐藏全帧 CPU 读回；4K 亚线性扩展证实瓶颈不在效果 shader。
- **export 的 ~59ms（1080p）/ ~300ms（4K）中绝大部分是 linear_f32 读回 + 编码**，不是求值（attribution：eval 2–5ms / readback 占大头）。降低导出耗时的工作应投向读回与编码管线（PBO 已有），不在节点层。
- **deflicker（77ms@1080p / 343ms@4K，唯一离群）**：时间域算法——每帧 R32F luma 读回 + CPU 环形缓冲百分位（`deflicker_node.py` 头部自述）。成本随分辨率线性、随环窗线性，属**算法性成本而非实现退化**。GL4.1 无 compute 使 GPU 环方案在本机不可用（Metal 桥 AB 测试结论：fragment 回退更快）。挂账：未来若上 GL4.3+/Metal 主路径，可把环缓冲与百分位移 GPU。
- **cpu_readback/cpu_raster 家族**（text/paint/particle/frame_interpolate/AI 族）：与静态分类一致，属已知边界，非回归。

## 四、发现并修复的问题

1. **[生产级崩溃] 并行视频导出 NameError（`LINEAR_SCENE`）**：`_run_parallel_video_stage` 内嵌 `_convert` 使用的 `LINEAR_SCENE` 只在 `render_video` 局部作用域导入——**任何 parallel_frames≥2 的视频导出在首帧色彩转换即崩**。已修（stage 头部补导入）+ AST 契约测试 + 2 帧真实 H.264 并行冒烟。
2. **[基准误报] mask_merge 全白被判 failed**：节点语义 `matte_value=max(alpha,luma)`，不透明照片输入数学上就是全覆盖。为基准骨架补软 matte fixture（复用 greenscreen/cryptomatte 的 NODE_FIXTURES 机制）后：mean=129/std=46.6，3.3ms —— 节点本身健康。
3. **[流程事故] 排查期间一次误 `git stash` 吞掉了基准脚本三处编辑与新 JSON**：已从 stash 恢复 JSON、重放三处编辑（跳过通用 mask 口接线/新节点登记/参数覆盖），并把 stash 清空留档。
4. **[确认非 bug] file_output 在序列渲染中零写入**：`render_image_sequence` 只求值选定命名输出的依赖集（"一次导一路"，CP-075 已挂账多输出），file_output 不在集合内。导出基准改直测写出器。

## 五、复现命令

```bash
# 提取真实帧（一次性）
ffmpeg -i tests/test_videos/4K_prores422LT.mov -vf "select='between(n,10,12)'" -fps_mode passthrough -frames:v 3 .cache/topos_comp_bench/real4k_f%d.png
ffmpeg -i tests/test_videos/2K_prores422HQ.mov -vf "select='between(n,10,14)'" -fps_mode passthrough -frames:v 5 .cache/topos_comp_bench/realhd_f%d.png
# 全节点基准（99 节点）
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python scripts/bench_all_composite_nodes_real_topos.py
# 4K 热点抽查
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python scripts/bench_composite_nodes_gpu_coverage.py --res 4k --node-filter deflicker,blur,glow,keyer,light_wrap
# 导出速度
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python scripts/bench_cp_export_real_media.py
```
