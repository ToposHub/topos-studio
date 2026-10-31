# 场景剪切探测 Bug 审计与优化计划

日期：2026-06-04  
范围：`src/features/edit/core/scene_cut/`、`src/features/edit/ui/scene_cut_*.py`、媒体池右键入口、Add Cuts to Media Pool、缓存、测试工具和 `tests/test_videos`

## 1. 结论

当前实现已经完成了大量基础模块：数据模型、检测器、worker、过滤器、缓存类、结果表、图表、弹窗、媒体池右键入口、子片段创建和项目序列化测试都已存在。  
但从实际链路看，还不能认为 Resolve-like 场景剪切探测已经打通。

最关键的问题有 5 个：

| 优先级 | 问题 | 当前影响 | 结论 |
|---|---|---|---|
| P0 | 预览窗口不显示视频帧 | 用户打开工具后看不到 before/current/after 画面 | 必修 |
| P0 | 用户接受/拒绝结果没有同步到 `SceneCutAnalysis` | `Add Cuts to Media Pool` 可能拿旧 analysis，添加失败或添加 0 个子片段 | 必修 |
| P0 | synthetic fixture 检测准确性失败 | `hard_cut` 等带期望剪切点的 fixture benchmark 全部 0 candidates | 必修 |
| P1 | 缓存写入和读取 key 不一致 | 第二次 benchmark 仍 `cache_hit=false`，缓存加速失效 | 必修 |
| P1 | 4K 性能未达标 | 4K H.264 只有约 0.69x-0.78x realtime，低于 2x 目标 | 必修 |

## 2. 本次验证结果

### 2.1 命令

```bash
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_models.py tests/edit/test_scene_cut_detector.py tests/edit/test_scene_cut_false_positive_guards.py tests/edit/test_scene_cut_cache.py tests/edit/test_scene_cut_timeline_mapping.py tests/edit/test_scene_cut_subclips.py tests/edit/test_scene_cut_project_serialization.py tests/edit/test_scene_cut_result_model.py tests/edit/test_scene_cut_worker.py tests/edit/test_scene_cut_apply_ops.py
```

结果：`539 passed, 6 xfailed, 15 xpassed in 36.67s`

说明：基础模块单测通过，但真实准确性测试被 `xfail` 标记，UI 预览链路没有被覆盖。

### 2.2 真实素材 benchmark

| 素材 | 结果 | 候选数 | 性能 | 缓存 |
|---|---:|---:|---:|---|
| `tests/test_videos/2K_h264.mp4` | 成功 | 9 | 约 1.81x / 2.06x realtime | 第二次仍 `cache_hit=false` |
| `tests/test_videos/2K_prores422HQ.mov` | 成功 | 1 | 约 1.48x / 1.50x realtime | 第二次仍 `cache_hit=false` |
| `tests/test_videos/2K_H265.mov` | 成功 | 0 | 约 1.40x / 1.49x realtime | 第二次仍 `cache_hit=false` |
| `tests/test_videos/4K_H264.mov` | 成功 | 0 | 约 0.69x / 0.78x realtime | 第二次仍 `cache_hit=false` |

说明：解码链路能跑，但真实缓存未命中，4K 性能明显不足。

### 2.3 Fixture benchmark

命令：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --all-fixtures --json
```

结果：8 个 fixture 全部运行成功，但 `avg_candidates=0.0`。其中：

| Fixture | 期望剪切点 | 实际候选 |
|---|---:|---:|
| `hard_cut` | `[100, 200]` | 0 |
| `dissolve` | `[100]` | 0 |
| `black_gap` | `[120, 240]` | 0 |
| `vfr_cut` | `[80, 200, 260]` | 0 |
| `duplicate_frames` | `[110]` | 0 |
| `black_fade` | `[140]` | 0 |
| `flash_no_cut` | `[]` | 0 |
| `fast_pan` | `[]` | 0 |

说明：无剪切 fixture 正确为 0，但有剪切 fixture 也为 0，算法 recall 不达标。

### 2.4 最小 GUI 验证

注入一个候选点后，弹窗选中第一行，三联预览内部状态为：

```python
{'current_index': 0, 'row_count': 1, 'preview_pixmaps': [None, None, None]}
```

说明：不是图片绘制失败，而是根本没有加载 `QImage/QPixmap`。

### 2.5 决策同步验证

用户在 model 中接受一个候选后：

```python
{'model_accepted': 1, 'analysis_accepted': 0, 'analysis_candidate_decision': 'pending'}
```

说明：UI model 和 `dialog._analysis` 的决策状态不一致。

## 3. 已完成度审计

| 模块 | 完成情况 | 主要缺口 | 证据 |
|---|---|---|---|
| 媒体池右键入口 | 基本完成 | 只负责打开弹窗，没有自动检测或预加载首帧 | `media_pool_panel.py` list/grid 均发 `scene_cut_detect_requested` |
| `SceneCutController` | 部分完成 | 未接缓存；未传 `asset.proxy_path`；Add Cuts 使用旧 analysis | `scene_cut_controller.py:368`、`:397` |
| `SceneCutDialog` | UI 壳完成 | 预览帧加载是 placeholder；不自动检测；手动 Add 未实现 | `scene_cut_dialog.py:1215` |
| 三联预览 | 控件完成 | 没有视频帧 provider；无异步 thumbnail loading | `SceneCutPreviewWidget.set_candidate()` 只消费传入图片 |
| 检测器 | 能跑 | fixture recall 失败；长素材会收集所有帧进内存；proxy path 未实际用于 sampler | `detector.py:330` |
| 过滤器 | 有规则 | 阈值/权重导致 hard cut fixture 漏检；flash guard 可能压掉真实切点 | `filters.py:309`、`:335` |
| 缓存类 | 有实现 | UI 不使用；benchmark 保存/读取 key 不一致 | `models.py:925` vs `benchmark_scene_cut_detection.py:280` |
| Add Cuts to Media Pool | 核心函数有 | UI 决策未同步，controller 忽略 accepted list | `result_model.py:323`、`scene_cut_controller.py:397` |
| 子片段模型 | 基本完成 | Timeline 插入子片段仍从 source_start=0 开始 | `timeline_view.py` `_add_asset_as_clip()` 固定 `source_start=0` |
| 测试 | 数量多 | 真实准确性被 xfail；UI 预览/完整链路缺少测试 | `test_scene_cut_false_positive_guards.py` 顶部 xfail |

## 4. P0 修复计划

### P0-1：修复预览窗口不显示

目标：打开 Scene Detect 后，选中候选点时，三联预览必须显示 cut 前一帧、cut 当前帧、cut 后一帧。

涉及文件：

| 文件 | 修改点 |
|---|---|
| `src/features/edit/ui/scene_cut_dialog.py` | `_select_candidate()` 不再只调用 `set_candidate(candidate)`，必须请求帧图像 |
| `src/features/edit/ui/scene_cut_preview_widget.py` | 保留现有纯显示职责，增加加载中/失败状态可选 |
| `src/features/edit/core/scene_cut/frame_sampler.py` | 提供稳定的单帧读取 API 或新增预览帧服务 |
| `src/features/edit/core/scene_cut/cache.py` | 缩略图缓存真正接入预览 |

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | 新增 `SceneCutPreviewFrameProvider`，输入 `source_path/settings/cache_key`，输出 `QImage` | 单测读取 `tests/fixtures/scene_cut/hard_cut_24fps.mp4` 第 99/100/101 帧 |
| 2 | 支持 BGR/RGB 到 `QImage.Format_RGB888` 的安全转换，避免 numpy buffer 生命周期失效 | `QImage.isNull() == False`，尺寸大于 0 |
| 3 | `_select_candidate()` 调用 provider，传入 before/current/after 三帧 | GUI offscreen 验证 `preview_pixmaps` 不为 None |
| 4 | 对 frame < 0、frame >= total_frames 做边界钳制 | 首帧候选不崩溃 |
| 5 | 加入 LRU 内存缓存和磁盘 thumbnail 缓存 | 重复选择同一候选不重复解码 |
| 6 | 长素材使用异步加载，旧请求可取消 | 快速上下切换候选不显示错帧 |

验收命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_scene_cut_preview_frames.py
```

人工验收：

1. 媒体池右键 `2K_h264.mp4`。
2. 打开 Scene Detect。
3. 点击 `Auto Scene Detect`。
4. 选择第一条候选。
5. 三个预览窗都显示画面，不再是 placeholder/黑窗。

### P0-2：修复接受/拒绝决策同步

目标：用户逐个接受/拒绝后，`Add Cuts to Media Pool` 使用的 analysis 必须包含最新 decision。

涉及文件：

| 文件 | 修改点 |
|---|---|
| `src/features/edit/ui/scene_cut_result_model.py` | 每次 decision 变化后生成新的 `SceneCutAnalysis` 或提供 `to_analysis()` |
| `src/features/edit/ui/scene_cut_dialog.py` | `_on_decision_changed()` 同步 `self._analysis` |
| `src/features/edit/core/scene_cut_controller.py` | 不再访问 `dialog._analysis` 私有字段；使用 dialog 提供的 updated analysis |

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | 给 `SceneCutResultModel` 增加 `to_updated_analysis()` | model accept 后 analysis.accepted_count 为 1 |
| 2 | `SceneCutDialog.get_analysis()` 返回带最新决策的 analysis | 最小 GUI 验证通过 |
| 3 | `cuts_added_to_pool` 信号改为发 updated analysis，或 controller 调用 `dialog.get_analysis()` | controller 不访问 `_analysis` 私有字段 |
| 4 | `Add Cuts to Media Pool` 前用 updated analysis 生成 N+1 子片段 | 1 个 accepted cut 生成 2 个 subclips |
| 5 | 增加端到端测试：accept -> add -> project assets 增加 | pytest 通过 |

验收命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_scene_cut_dialog_add_cuts_flow.py
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_subclips.py tests/edit/test_scene_cut_project_serialization.py
```

### P0-3：修复 fixture 检测准确性

目标：`hard_cut`、`vfr_cut`、`black_gap`、`duplicate_frames` 等 fixture 达到可用 recall，`flash_no_cut`、`fast_pan` 不产生高置信 hard cut。

当前关键证据：

| 帧 | combined_score | threshold | 结果 |
|---:|---:|---:|---|
| hard_cut frame 100 | 0.0939 | 0.15 | 低于阈值，漏检 |
| hard_cut frame 200 | 0.1549 | 0.15 | 过阈值，但 flash_score=0.4176，可能被 suppress |

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | 调整 histogram normalization：不要将 chi-square 再除以 `HIST_BINS` 到过低 | 红/蓝/绿纯色切换 hist_delta 明显高于普通帧 |
| 2 | 增强 hue/color distance 权重，降低纯色硬切漏检 | `hard_cut` 检出 100、200 |
| 3 | flash guard 需要看“三帧模式”：亮度突升后回落才是 flash，单向进入新场景不能直接 suppress | frame 200 不被误压 |
| 4 | adaptive threshold 对全零背景下的孤立峰值使用 top-k/percentile 补偿 | `threshold` 不高于真实峰值 |
| 5 | 将 `test_scene_cut_false_positive_guards.py` 中核心准确性从全局 xfail 改为真实必过，保留少量已知难题单独 xfail | CI 能防止回归 |
| 6 | benchmark 增加 precision/recall/F1，不只报告 candidates_found | all-fixtures 输出准确率 |

验收命令：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --all-fixtures --json
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_false_positive_guards.py
```

最低验收标准：

| Fixture | 目标 |
|---|---|
| `hard_cut` | recall >= 1.0，precision >= 0.95 |
| `flash_no_cut` | 0 个 accepted/high-confidence hard cut |
| `fast_pan` | 0 个 accepted/high-confidence hard cut |
| `black_gap` | 至少定位到黑场后新场景起点，误差 <= 5 帧 |
| `vfr_cut` | 映射误差 <= 2 帧 |

## 5. P1 修复计划

### P1-1：修复缓存无法命中

问题：读取时使用 `generate_cache_key(...)`，保存时 `SceneCutCache.save_analysis()` 使用 `analysis.to_cache_key()`，两套 key 组成不同。

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | 统一 cache key：`AnalysisCache.save_analysis(analysis, cache_key=None)` 支持传入外部 key | benchmark 第 2 次 `cache_hit=true` |
| 2 | `SceneCutAnalysis.to_cache_key()` 改为复用 `generate_cache_key` 所需组件，或删除双轨 key | key 单测固定 |
| 3 | 修复 cached path 中 `len(cached_analysis.source_time_index)`，应使用 `.total_frames` 或 `len(points)` | cache hit 时不崩溃 |
| 4 | UI worker 启动前先查 cache，命中则直接 `set_analysis()` | 同素材二次打开无需重新检测 |
| 5 | 修改 sensitivity/proxy/source range 后必须重新生成 key | 设置变化不会误用旧缓存 |

验收命令：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --media tests/test_videos/2K_h264.mp4 --json
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_cache.py
```

目标：第二次 run `cache_hit=true`，`cache_load_seconds < 1.0`。

### P1-2：接入代理素材加速

问题：controller 默认 `use_proxy=True`，但创建 `SceneCutDialog/SceneCutWorker` 时没有传 `asset.proxy_path`，detector 也没有把 proxy path 交给 sampler。

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | `DetectionContext` 增加 `proxy_path` | ready proxy asset context 有路径 |
| 2 | `SceneCutController` 如果 `asset.has_proxy` 且设置 `use_proxy=True`，传 proxy path | worker.proxy_path 不为 None |
| 3 | `SceneCutDetector` 实际使用 `proxy_path or source_path` 创建 sampler | diagnostics.decode_path 为 `proxy` |
| 4 | 增加 proxy->master frame mapping，不能只按 proxy frame index 直接写回 master | 不同 fps proxy 映射准确 |
| 5 | cache key 纳入 proxy fingerprint | proxy 变化后缓存失效 |

验收命令：

```bash
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_proxy_mapping.py
.venv/bin/python tools/benchmark_scene_cut_detection.py --media tests/test_videos/4K_H264.mov --use-proxy --json
```

### P1-3：优化长素材和 4K 性能

问题：`SceneCutDetector.detect()` 当前把所有 `SampledFrame` 放入 `sampled_frames`，再生成完整 time index。长素材会有内存和速度风险。

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | 改为 streaming pipeline：只保留前一帧、metrics entry 和 time point，不保存所有 frame_data | 1 小时素材内存增长 < 300 MB |
| 2 | metrics 使用批量/向量化优化，减少 cv2 重复转换 | 2K >= 4x realtime |
| 3 | 对预览用独立 thumbnail provider，不复用检测时所有帧数据 | 检测完成后内存可释放 |
| 4 | 增加采样步长策略：粗扫 + cut 附近精扫 | 长素材检测速度提升 |
| 5 | 4K 默认走 proxy 或更低 sample width，UI 明确显示正在使用 proxy/master | 4K no-proxy >= 2x realtime，proxy >= 8x realtime |

验收命令：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --media tests/test_videos/4K_H264.mov --json
.venv/bin/python tools/benchmark_scene_cut_detection.py --media tests/test_videos/4K_H264.mov --use-proxy --json
```

## 6. P2 修复计划

### P2-1：打开弹窗后的交互体验

| 问题 | 优化 |
|---|---|
| 右键打开后空窗口，用户要手动点 Auto Detect | 可配置为打开后自动查缓存；无缓存时显示首帧和醒目的 Auto Scene Detect 状态 |
| 检测完成弹 `QMessageBox` 会打断 review | 改为底部状态条/非阻塞 toast |
| transport play 是 placeholder | 先实现逐帧 seek 和跳转 cut；播放可后续接 SourceViewer |
| manual Add 是 placeholder | 在当前 frame 创建 `SceneCutCandidate` 并插入 model/graph |

### P2-2：子片段进入时间线的 source range

问题：`_add_asset_as_clip()` 创建 `ClipInstance` 时固定 `source_start=0`，即使 asset 是 subclip，也可能从原素材开头插入。

实施步骤：

| 步骤 | 内容 | 验证 |
|---|---|---|
| 1 | timeline 插入前判断 `metadata.is_subclip` | subclip asset 走专用 source range |
| 2 | `source_start=metadata.source_start`，`source_duration=metadata.source_duration` | 拖入 timeline 后 source range 正确 |
| 3 | timeline duration 用 subclip duration 映射到 timeline fps | 不同 fps 下长度正确 |
| 4 | 缩略图和 waveform 使用子片段范围 | UI 不显示原素材开头 |

验收命令：

```bash
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_subclip_timeline_insert.py
```

### P2-3：VFR、掉帧、不同 fps 映射

| 项目 | 优化 |
|---|---|
| VFR time index | 使用 PTS 构建 source frame <-> seconds 映射，避免 `frame/fps` 简化 |
| proxy 映射 | proxy candidate seconds -> master nearest source frame |
| drop frame | 增加 timecode formatting 测试，不把 drop-frame TC 当普通帧号 |
| timeline clip 检测 | `start_detection_for_clip()` 目前 TODO，必须读取 clip 的 `source_start/source_duration` |

验收命令：

```bash
.venv/bin/python -m pytest -q tests/edit/test_scene_cut_vfr_mapping.py tests/edit/test_scene_cut_timeline_mapping.py
```

## 7. 新增测试计划

| 测试文件 | 目标 |
|---|---|
| `tests/edit/test_scene_cut_preview_frames.py` | 验证候选选择后 before/current/after 三帧非空 |
| `tests/edit/test_scene_cut_dialog_add_cuts_flow.py` | 验证 accept/reject 同步到 updated analysis，并能添加子片段 |
| `tests/edit/test_scene_cut_cache_integration.py` | 验证 UI/benchmark 第二次命中缓存 |
| `tests/edit/test_scene_cut_proxy_mapping.py` | 验证 proxy 检测结果映射回 master frame |
| `tests/edit/test_scene_cut_subclip_timeline_insert.py` | 验证子片段拖入 timeline 使用 source range |
| `tests/edit/test_scene_cut_fixture_accuracy.py` | 将 fixture accuracy 从 xfail 提升为核心验收 |

## 8. 推荐实施顺序

| 顺序 | 任务 | 原因 |
|---:|---|---|
| 1 | P0-1 预览帧加载 | 直接解决当前用户可见 bug |
| 2 | P0-2 决策同步和 Add Cuts | 打通 Resolve-like 核心流程 |
| 3 | P1-1 缓存命中 | 避免重复检测，提高体验 |
| 4 | P0-3 fixture 准确性 | 没有准确性，UI 做得再像也不可用 |
| 5 | P1-2 proxy 接入 | 解决长素材和 4K 速度 |
| 6 | P2-2 subclip timeline source range | 保证媒体池子片段真正可剪辑 |
| 7 | P1-3 streaming 性能 | 面向长素材稳定性 |
| 8 | P2-3 VFR/fps/drop frame | 提升专业可靠性 |

## 9. Definition of Done

完成后必须同时满足：

1. 媒体池右键视频素材能打开 Scene Detect。
2. 打开后至少能显示首帧；检测完成后选择候选能显示 before/current/after 三帧。
3. `Auto Scene Detect` 对 `hard_cut` fixture 检出 `[100, 200]`。
4. 用户接受/拒绝的决策同步到 updated analysis。
5. `Add Cuts to Media Pool` 对 1 个 accepted cut 生成 2 个虚拟子片段。
6. 子片段保存/加载后 metadata 保持完整。
7. 子片段拖入 timeline 使用自己的 `source_start/source_duration`。
8. 第二次打开同素材同设置时命中缓存，加载小于 1 秒。
9. 4K H.264 no-proxy 至少达到 2x realtime，proxy 至少达到 8x realtime。
10. `tests/edit/test_scene_cut_false_positive_guards.py` 的核心准确性不再依赖全局 xfail。

