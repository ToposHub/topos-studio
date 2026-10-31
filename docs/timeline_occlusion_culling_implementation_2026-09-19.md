# Timeline 遮挡裁剪实施记录

> 实施日期：2026-09-19
>
> 对应计划：`docs/timeline_occlusion_culling_optimization_plan_2026-09-19.md`
>
> 分支：`bugfix`（O0+O1 → O2 → O3 → O4+O7 → 真实素材回归 → O8 分批提交）

## 1. 实施范围与状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| O0 基线 telemetry | ✅ 完成 | `OcclusionTelemetry`（线程安全滑动窗口 + `snapshot()` 聚合），预览/导出统一写入；`TOPOS_TIMELINE_OCCLUSION_DEBUG=1` 逐帧日志 |
| O1 共享纯分析模块 | ✅ 完成 | `src/features/edit/core/timeline_occlusion.py`，纯函数、fail-closed、稳定 `reason` 字符串 |
| O2 CPU 预览接入 | ✅ 完成 | `_prune_occluded_layers_for_preview` 收敛到共享模块；GPU/CPU 预览共用 `_execute_render` 入口的裁剪结果 |
| O3 CPU/批量导出接入 | ✅ 完成 | `TimelineExporter._composite_layers` 唯一漏斗接入；嵌套序列递归受益 |
| O4 GPU 预览/导出接入 | ✅ 完成（审计+回归验证） | GL/Metal 导出经 `_gpu_render_composite(sorted_layers)` 消费裁剪后列表；实时播放 scheduler 配置同源裁剪；批量导出（`video_export_job`）复用 `TimelineExporter` |
| O5 矩形覆盖分析 | ✅ 完成（默认关） | 90° 倍旋转 + 1 像素安全膨胀；`TOPOS_TIMELINE_OCCLUSION_RECT=1` 开启 |
| O6 ROI 数据契约 | ✅ 完成（默认关） | `compute_visible_region_plan` 只输出不消费；`TOPOS_TIMELINE_OCCLUSION_ROI=1` |
| O7 效果能力声明 | ✅ 完成 | `EffectSpec.background_independent`（默认 False）；`.tglsl` 插件 `// @occlusion background_independent` 注解；第一阶段不改变裁剪行为 |
| O8 基准与文档 | ✅ 完成 | `tests/benchmark/benchmark_timeline_occlusion.py`（B0–B4）；本文档 |

## 2. 安全语义（与计划 §3 对应）

fail-closed 条件（任一命中即保留下层，`reason` 记入 telemetry）：

- `transition_present` / `layer_in_transition` — 帧内有任何转场
- `opacity_below_one` / `opacity_invalid` — 有效 opacity < 1（含 fade/关键帧）
- `unknown_alpha` — 源像素格式未知或带 Alpha（覆盖注册表缺口：`yuva*`、`rgba*`、`topos_yuva*` 名称识别）
- `transform_not_identity` — 非恒等变换（O5 开启时接受"90° 倍旋转 + 覆盖画布+1px 膨胀"）
- `crop_present` — 任何边裁切或羽化
- `blend_mode:<mode>` — 非 Normal 图层混合模式
- `effects_present` — 挂载任何启用中的视频效果
- `composite_clip` / `asset_kind:<kind>` — 合成绑定、adjustment、solid 等类型不可作遮挡层
- `facts_unavailable` / `analysis_error` — 事实收集失败或分析异常，回退保留全部层

扫描语义：从上到下找到第一个**确定性全画布不透明**层；只裁掉它**下方**的层。最底层即使满足不透明条件也不构成遮挡结论（无可裁对象）。

## 3. 环境开关（§8）

| 变量 | 默认 | 作用 |
|---|---|---|
| `TOPOS_TIMELINE_OCCLUSION_CULL` | `1` | 总开关；画面异常时可单独关闭，不影响其它优化 |
| `TOPOS_TIMELINE_OCCLUSION_RECT` | `0` | O5 矩形覆盖分析（放大仍覆盖全画布） |
| `TOPOS_TIMELINE_OCCLUSION_ROI` | `0` | O6 ROI 数据契约消费（当前只输出） |
| `TOPOS_TIMELINE_OCCLUSION_DEBUG` | `0` | 每帧输出 total/render/culled/occluder/reason/耗时 |

## 4. 真实素材回归（§6.2）

`tests/edit/test_timeline_occlusion_real_media.py`（真实 FFmpeg/ProRes 解码，无 GUI）：

| 组 | 时间线 | 验收 | 结果 |
|---|---|---|---|
| B1 | 两轨，上轨 ProRes422HQ 全屏 | 下轨取帧请求 0；开/关逐像素一致 | ✅ 3 轮 |
| B2 | 两轨，上轨缩小 50% | 两层均请求；逐像素一致 | ✅ 3 轮 |
| B3 | 四轨，最上层全屏 | 下面三轨请求 0；逐像素一致 | ✅ 2 轮 |
| B4 | 同轨 cross_dissolve | 转场帧两层全保留；逐像素一致 | ✅ |
| B5 | 上轨 Alpha 元数据 | fail-closed 保留下层；逐像素一致 | ✅ |
| §6.2-5 | 两轨全屏遮挡 + 第 2 帧取消 | 被裁下轨从未打开解码会话；取消后全部会话释放无泄漏 | ✅ |
| §6.3-1 | 两轨全屏遮挡，CPU preview vs CPU export | 同帧 PSNR ≥ 40 dB（真实双解码源） | ✅ |
| B6/§6.3-4 | 同工程两次 `export_timeline`（单文件与批量共用该漏斗） | 裁剪决策逐帧一致（帧数/被裁数/原因分布相同）；输出解码帧 max diff ≤ 1 | ✅ |

连续多次完整运行全绿（约 13–18s/轮，1920×1080@25fps，2K_h264.mp4 + 2K_prores422HQ.mov）。

## 5. 实机基准（§7）

运行方式（ON/OFF 逐轮交错，消除缓存冷启动偏差）：

```bash
# 计划口径：预热 60 帧 / 统计 480 帧 / 3 轮取中位数
.venv/bin/python tests/benchmark/benchmark_timeline_occlusion.py
# 快速抽检
.venv/bin/python tests/benchmark/benchmark_timeline_occlusion.py --warmup 6 --measure 24 --runs 1
```

正式口径（60/480/3，ON 先跑的首版数据）结果（CPU 合成路径，2K 素材）：

| 组 | OFF fps | ON fps | 加速 | 总请求降幅 | 下轨请求降幅 | 门槛 |
|---|---:|---:|---:|---:|---:|---|
| B0 单轨 | 86.8 | 77.4 | 0.89x→**1.00x**† | 0% | — | ≤1% 回归 ✅ |
| B1 两轨全屏 | 11.3 | 36.7 | **3.26x** | 50% | **100%**（540→0/测量窗） | ≥90% ✅ |
| B2 缩小 50% | 8.0 | 8.0 | 1.00x | 0%（不误裁） | 0% | 不得跳过下轨 ✅ |
| B3 四轨全屏 | 7.0 | 36.6 | **5.23x** | 75% | **100%**（1620→0） | ≥75% ✅ |
| B4 上轨 opacity=0.5 | 11.6 | 11.5 | 0.99x | 0%（不误裁） | 0% | ✅ |

† 首版脚本 ON 固定先跑产生缓存冷启动偏差；ON/OFF 逐轮交错 + 5 轮复测 B0 为 **1.00x**（87.8 vs 87.6 fps）。分析器逐帧开销微基准：单层 3.5µs、四层 10.6µs（含 facts 回调），占 3–11.5ms 真实帧合成时间的 **0.09%–0.35%**，远低于 1% 门槛。

正式口径结果 JSON 见 `benchmarks/timeline_occlusion_bench_result.json`（每次运行覆盖）。

**B5 4K 档位**（`--material 4k`，4K_H264.mov + 4K_prores422HQ.mov，3840×2160，缩减口径 10/60/2，结果落 `timeline_occlusion_bench_result_4k.json`）：

| 组 | OFF fps | ON fps | 加速 | 总请求降幅 | 下轨请求降幅 |
|---|---:|---:|---:|---:|---:|
| B0 单轨 | 23.7 | 24.2 | 1.02x | 0% | — |
| B1 两轨全屏 | 3.1 | 11.5 | **3.67x** | 50% | **100%** |
| B2 缩小 50% | 2.1 | 2.1 | 0.99x | 0%（不误裁） | 0% |

4K 收益不低于 2K（3.67x vs 3.26x），B1 下轨解码请求降幅同为 100%——§11"4K 多轨基准达到收益门槛"满足（B1 ≥90% 门槛组）。2K 档即近似 1080p proxy 对照（proxy/原片请求降幅一致，均由遮挡语义决定而非分辨率）。

## 6. 已知限制

1. 第一阶段遮挡层必须是**恒等几何 + 无 Crop/羽化 + Normal + 无效果 + 已知无 Alpha 的 video/image**；solid/generator 虽然视觉上覆盖画布，但按计划 §3.1 不作为遮挡证据（fail-closed，仅损失优化机会）。
2. 任意角度旋转的层永远不能作遮挡层（外接矩形 ≠ 实际覆盖）；O5 仅放行 90° 倍旋转。
3. 效果链 `background_independent` 声明在第一阶段**不改变行为**——任何挂载效果仍阻止其层成为遮挡层；声明供未来高级裁剪模式消费。
4. O6 ROI 契约只输出 `RegionPlan`（单矩形覆盖/完全遮挡/整画布三态），尚未接入 ROI 解码。
5. telemetry 是进程级滑动窗口（8192 帧），重启即清空；导出/预览共用同一实例。
6. `source_alpha_known_opaque` 依赖资产 metadata 的 `pixel_format` 字段；元数据缺失的旧工程按未知 Alpha 处理（保守保留，不会误裁）。
7. ~~计划 §7 的 B5/B6 基准组未运行~~ → **第三轮已补**：B5 4K 档（CPU 维度，见 §5）与 B6（漏斗审计 + 重复导出确定性测试）已关闭；B5 的 **GPU/显存收益维度**仍需实机（headless 无 GL/Metal 上下文）。
8. ~~CPU preview 与 CPU export 的同帧 PSNR 未执行~~ → **第三轮已补**（真实双解码源，≥40 dB）。仍待实机的对拍：CPU preview vs GPU preview、CPU export vs GPU/Metal export（§6.3 后两行）——两侧裁剪决策同源（共享模块）已由代码结构与接缝测试保证，像素级 GPU 对拍需 GUI/实机环境。
9. O5/O6 机制已实现且有完整单测，但按计划 §8 规则 1 **默认关闭**，实机开启验证未做——这是计划设计的独立验证阶段，非缺陷。
10. O7 无内置效果或插件实际声明 `background_independent`（第一阶段按计划不放行任何效果，机制与 fail-closed 单测已就绪）。

## 6a. 复查（2026-09-19 第二轮）发现与修复

整体复查（计划逐项 + 调用点归属 + 语义边界）发现并修复了 4 个问题：

| # | 问题 | 严重性 | 修复 |
|---|---|---|---|
| 1 | **存量缺陷**：`TimelinePreviewRenderer._build_realtime_scheduler_config` 调用 `self._prune_occluded_layers_for_preview`，但该方法只定义在 `PreviewRenderWorker` 上——必然 `AttributeError` 且被 `except Exception: return None` 静默吞掉。该方法经 `config_provider` 真实接线到实时播放引擎，引擎拿到 None 即跳过调度器重配置 → **实时解码调度自该调用引入起一直在静默降级**（与裁剪无关的存量回归，恰好落在计划 O4"realtime_playback 只接收裁剪后的渲染任务"范围内） | 高（存量） | 改为委托 `worker._prune_occluded_layers_for_preview`（worker 在该点保证非 None）；新增回归测试证明配置路径返回有效 dict 且裁剪生效 |
| 2 | 导出 facts 的效果统计口径（"启用中"）与预览（"任意已知效果"，含禁用）不一致；且预览 CPU 效果链存在**不跳过禁用效果**的存量行为——若导出按启用统计，会造成预览/导出裁剪决策分歧 | 中 | 导出统一为"clip 上挂载任何效果均阻止遮挡"（与预览同口径、双重保守） |
| 3 | `_analyze_impl` 排序用 `getattr(l, "track_index", 0)`：鸭子类型层缺失该属性时被默认 0 排到最底，可能把真正的上层错误裁掉（违反 fail-closed） | 中（理论） | 改为直接属性访问，缺失即抛异常落入 `analysis_error` 保留全部层；`compute_visible_region_plan` 同步加异常回退（保守整画布）+ 单测 |
| 4 | 预览嵌套序列路径（`_render_nested_sequence_frame`）直接调 `_composite_layers`，绕过裁剪（导出的嵌套路径经 `_composite_layers` 内部分析有裁剪）——无正确性问题，但预览/导出嵌套裁剪不一致 | 低（优化缺口） | 嵌套求值前对子序列层做同一裁剪（`record=False` 防遥测双计） |

复查同时**确认安全**的边界（重点核查过）：

- **Planar 直通顺序**：`_try_render_planar_passthrough` 在 `_composite_layers`（裁剪发生处）**之前**执行、看到的是未裁剪 plan——多轨时间线不会被裁剪"变成"直通资格，无语义突变。
- **Alpha 导出 + 裁剪**：不透明全画布 occluder 的 over 合成 `out_a = 1`，裁掉下层前后逐像素等价。
- **调色/降噪不阻止遮挡**：ColorStack/降噪只改 occluder 自身像素，不产生透明或扩大覆盖；效果阻止遮挡是按计划 §3.1 的保守策略（效果可能产生透明语义）。
- **负缩放（镜像）**：矩形数学取 min/max，镜像全画布仍正确判定覆盖。
- **裁剪后 layers 非空**：`render_layers` 至少含 occluder，`_execute_render` 的空层守卫不可达。
- **adjustment 层语义**：occluder 之上的 adjustment 保留并按 z 序后处理；之下的随遮挡裁掉（其作用对象本就不可见）。
- **`test_async_prefetch_is_latest_only`**：基线（16ee5038）与本分支以相同方式各跑 5 次均随机波动（2/5 vs 1/5 通过）——存量偶发时序测试，与本改动无关（该测试不导入任何被改模块）。

## 6b. 第三轮核对（2026-09-19）：计划逐项遗漏清零

对计划表全文逐条核对后补齐了 4 项遗漏（全部真实素材）：

1. **§6.2-5 取消/资源清理**：两轨全屏遮挡导出在第 2 帧取消——被裁下轨从未打开解码会话（进度回调逐帧断言），取消收尾后 `_open_clips` 清空无泄漏。
2. **§6.3-1 CPU preview vs CPU export PSNR**：真实 worker（GPU 关闭，直连预览解码源）与真实导出器（导出会话解码源）同帧对拍 ≥40 dB。
3. **B6 单文件 vs 批量**：两入口（deliver_controller→VideoExporter、video_export_job）均汇入 `TimelineExporter.export_timeline`（代码审计），同工程重复导出决策逐帧一致 + 输出解码帧 max diff ≤1。
4. **§7-B5 4K 基准**：`--material 4k` 档位，B1 3.67x / 下轨请求 100% 消除 / B0 1.02x / B2 0.99x（见 §5 表）。

仍受环境约束、无法 headless 完成的计划条目（非缺陷，见"已知限制"7–10）：GPU/Metal 像素级对拍（§6.3 后两行）、B5 的 GPU/显存维度、O5/O6 实机开启验证、O7 效果放行。

## 7. 最终验收清单核对（§11）

- [x] 上轨全屏不透明时，下轨不再请求视频帧（B1/B3 真实素材，2K+4K）
- [x] 上轨缩小后，下轨仍参与计算（B2；导出四角像素断言在接缝测试覆盖）
- [x] 移动、旋转、裁剪、Alpha、opacity、转场、非 Normal 混合不被误裁（§6.1 单测 57 项 + B4/B5）
- [x] 预览、导出、GPU 回退、批量导出同一裁剪决策（共享 `timeline_occlusion`，O4 审计无旁路；B6 重复导出确定性）
- [x] CPU 输出与未优化基线逐像素对拍（开/关开关对拍）；CPU preview vs CPU export PSNR ≥40 dB（§6.3-1）
- [x] 取消、暂停、失败路径无资源泄漏（§6.2-5 取消清理测试；被裁层从未打开会话）
- [x] 分析异常回退保留全部层（`analysis_error`/facts 失败用例）
- [x] telemetry 显示实际减少的 layer decode/render 工作（`culled_layers`/`culled_ratio`/请求计数）
- [x] 基准达门槛（B1/B3 下轨请求 100% 消除；B0 无回归；4K 档 B1 3.67x）
- [x] 可通过 `TOPOS_TIMELINE_OCCLUSION_CULL=0` 关闭裁剪
- [x] 文档、测试、基准结果同步更新（本文档）
- [ ] （实机待验）GPU preview/export 像素级对拍与 B5 GPU/显存维度——需 GUI/Metal 环境
