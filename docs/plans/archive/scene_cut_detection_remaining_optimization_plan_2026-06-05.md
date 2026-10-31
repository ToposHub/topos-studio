# Scene Cut Detection 剩余问题优化计划表

创建日期：2026-06-05  
范围：Resolve-like 场景剪切探测工具剩余问题收口  
当前验证基线：

- Targeted 回归：`594 passed, 4 xfailed`
- 单项回归：`264 passed, 4 xfailed`
- Offscreen UI 验证：三联预览已显示 `99/100/101` 帧，接受候选后 `accepted_count=1`
- Fixture benchmark：8 个 fixture 全部成功，平均约 `5x realtime`
- 真实素材缓存 benchmark：`tests/test_videos/2K_h264.mp4` 缓存命中低于 1 秒

## 1. 总结

主链路已经打通：素材右键打开场景剪切探测、检测候选、三联预览、接受/拒绝同步、缓存 benchmark、fixture 基础识别都已可用。

剩余问题集中在 5 类：

1. 算法分类还不够干净，项目仍有 4 个显式 `xfail` 验收项。
2. 虚拟 subclip 拖入时间线时 source range 仍可能错误。
3. UI 正式检测缓存没有完整闭环，尤其 proxy cache key 和保存时机。
4. proxy 检测后的帧号映射还缺严格验证，VFR/掉帧/不同 fps 风险仍在。
5. 长素材与 4K/代理性能还缺完整验收矩阵。

## 2. 剩余问题总表

| ID | 优先级 | 问题 | 当前证据 | 风险 | 目标结果 |
|---|---|---|---|---|---|
| SC-REM-01 | P0 | Dissolve 软转场分类仍是 `xfail` | `TestTransitionRecognition::test_dissolve_fixture_soft_transition` xfail | 转场被当硬切或警告，用户需要手动清理 | dissolve fixture 稳定归为 `SOFT_TRANSITION`，不产生错误硬切 |
| SC-REM-02 | P0 | Black gap 分类仍是 `xfail` | `TestBlackGapHandling::test_black_gap_fixture_classification` xfail | 黑场前后切点/黑场段识别不稳定 | 黑场入口/出口和 black gap 类型稳定 |
| SC-REM-03 | P0 | Black gap 随机 cut 抑制仍是 `xfail` | `TestBlackGapHandling::test_black_gap_not_random_cuts` xfail | 黑场附近产生多余候选，影响媒体池片段质量 | 黑场区域只保留必要 cut，不产生随机峰值 |
| SC-REM-04 | P0 | 转场识别验收仍是 `xfail` | `TestAcceptanceMatrix::test_transition_recognition_acceptance` xfail | 综合验收仍不能标记为完成 | 移除 xfail，转场识别矩阵通过 |
| SC-REM-05 | P0 | Subclip 上时间线 source range 可能错误 | `timeline_view.py::_add_asset_as_clip()` 仍写死 `source_start=0` | 媒体池切出的片段拖到时间线后可能从母素材 0 帧播放 | 如果 asset 是 subclip，clip 使用 metadata 中的 `source_start/source_duration` |
| SC-REM-06 | P0 | UI 检测缓存没有保存闭环 | dialog 只有 `_try_load_cache()`，未在 `_on_detection_completed()` 保存分析 | 用户第二次打开仍可能重新检测 | UI 检测完成后按同一 cache key 保存分析 |
| SC-REM-07 | P0 | Proxy cache key 不一致 | `_try_load_cache()` 未传 `_proxy_path`，`SceneCutAnalysis.to_cache_key()` 固定 `no-proxy` | proxy/master 结果互相污染或无法命中 | cache key 统一包含 proxy fingerprint |
| SC-REM-08 | P1 | Proxy 检测帧号需映射回 master | worker 已接收 proxy path，但没有 proxy/master frame index 对齐验证 | 低帧率代理或 VFR 代理可能切点偏移 | proxy 检测结果精确映射回原素材帧号 |
| SC-REM-09 | P1 | VFR 单帧 seek 仍基于 fps 推算 PTS | `sample_single_frame()` 用 `frame_index / fps / time_base` seek | VFR/掉帧素材预览可能偏移 | seek 优先使用 `SourceFrameTimeIndex` 的 PTS/seconds |
| SC-REM-10 | P1 | 预览帧加载在 UI 线程同步执行 | `_select_candidate()` 直接 `get_preview_frames()` | 长 GOP/网络盘/4K 素材选择候选时卡顿 | 异步缩略图加载，当前候选先显示 loading，完成后替换 |
| SC-REM-11 | P1 | 真实素材候选 warning 偏多 | `2K_h264.mp4` 缓存结果 23 candidates / 23 warnings | UI 看起来“不确信”，用户审核负担大 | 高可信硬切自动更清晰，疑似误判只标记真正模糊项 |
| SC-REM-12 | P1 | 4K no-proxy 性能目标未重新验收 | 上轮 4K 约 0.69x-0.78x realtime，修复后未完整重跑 | 长素材/4K 不达 Resolve-like 体验 | 4K no-proxy 达到或接近 2x realtime，或自动建议 proxy |
| SC-REM-13 | P1 | 1080p proxy 8x realtime 未建立标准测试 | benchmark 目标显示 false，因为没有 1080p proxy 场景 | 代理加速只是代码接通，未被验收 | 有 proxy fixture/真实素材 benchmark，目标可判定 |
| SC-REM-14 | P1 | all-fixtures benchmark 的 cache 目标显示 false | fixture benchmark 默认 `use_cache=False` | 报表误导，以为 cache 不达标 | fixture 报表不校验 cache，或单独增加 cache benchmark |
| SC-REM-15 | P2 | 检测完成后弹出 modal message | `_on_detection_completed()` 调 `QMessageBox.information()` | 长流程/自动检测不流畅 | 改为非阻塞状态提示或底部 toast |
| SC-REM-16 | P2 | Add Cuts to Media Pool 全链路缺 GUI 集成测试 | 目前只验证 dialog 决策同步，未覆盖真实 project/media pool 变化 | 回归时媒体池片段可能漏加或排序错误 | 测试覆盖右键入口、检测、接受、添加到媒体池 |
| SC-REM-17 | P2 | 时间线标记/高亮疑似误判缺端到端验收 | core apply_ops 有测试，UI 链路未完整覆盖 | 用户无法直观看到疑似误判/标记 | 图表、列表、时间线 marker 三处状态一致 |

## 3. P0 实施步骤

| 步骤 | 对应问题 | 修改文件 | 具体做法 | 验证方法 |
|---|---|---|---|---|
| 1 | SC-REM-05 | `src/features/edit/ui/timeline_view.py` | 在 `_add_asset_as_clip()` 中检测 `asset.metadata["is_subclip"]`；读取 `source_start/source_duration`；video/audio linked clip 都使用同一 source range | 新增 `tests/edit/test_scene_cut_subclip_timeline_insert.py`，断言 subclip 拖入后 `ClipInstance.source_start` 等于母素材切点 |
| 2 | SC-REM-06 | `src/features/edit/ui/scene_cut_dialog.py` | `_on_detection_completed()` 成功后生成 cache key 并 `SceneCutCache().save_analysis()`；保存失败只记录日志，不阻塞 UI | Offscreen 检测一次后重新创建 dialog，第二次 `_try_load_cache()` 命中 |
| 3 | SC-REM-07 | `models.py`、`cache.py`、`scene_cut_dialog.py`、`benchmark_scene_cut_detection.py` | 移除 `SceneCutAnalysis.to_cache_key()` 内固定 `no-proxy`；新增 `cache_key` 或 `proxy_fingerprint` 字段，保存/读取统一走 `generate_cache_key()` | `tests/edit/test_scene_cut_cache.py` 新增 proxy/master 不串 cache、proxy 文件 mtime 改变后 cache miss |
| 4 | SC-REM-01 | `filters.py`、`metrics.py` | 调整 soft transition 判定：连续 N 帧中等差异、无单帧尖峰、亮度/色彩渐变一致时归类 soft transition | 解除 `test_dissolve_fixture_soft_transition` 的 xfail 并通过 |
| 5 | SC-REM-02/03 | `filters.py` | 黑场检测改为区间识别：低亮度连续区间入口/出口合并，区间内部峰值抑制 | 解除两个 black gap xfail 并通过 |
| 6 | SC-REM-04 | `tests/edit/test_scene_cut_false_positive_guards.py` | 在前 4/5 步稳定后移除 acceptance matrix xfail | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_scene_cut_false_positive_guards.py` 无 xfail |

## 4. P1 实施步骤

| 步骤 | 对应问题 | 修改文件 | 具体做法 | 验证方法 |
|---|---|---|---|---|
| 1 | SC-REM-08 | `detector.py`、`timeline_mapping.py` | 检测使用 proxy 时同时建立 proxy time index 和 master time index；候选按 seconds/PTS 映射回 master source frame | 构造 12fps proxy + 24fps master fixture，cut at master 100/200，检测结果仍返回 100/200 |
| 2 | SC-REM-09 | `frame_sampler.py` | `sample_single_frame()` 支持传入 time index；优先用目标 frame 对应 PTS seek，失败再 fps 推算 | 新增 VFR seek 单测，断言预览帧和 `SourceFrameTimeIndex` frame/PTS 一致 |
| 3 | SC-REM-10 | `scene_cut_dialog.py`、新增 `scene_cut_thumbnail_worker.py` | 预览加载改为后台 worker；选择候选时取消旧请求；返回时校验 candidate index 未变 | GUI 单测连续快速选择 20 个候选，无 UI 阻塞、最终只显示最后一个候选 |
| 4 | SC-REM-11 | `filters.py`、`models.py` | 重新定义 warning 条件：只对低 confidence、flash/fast motion/black fade 附近、过密 cut 标 warning | `2K_h264.mp4` warnings 比例显著下降，fixture false positive 不上升 |
| 5 | SC-REM-12 | `detector.py`、`frame_sampler.py` | 复核采样分辨率、PyAV decode threading、metric 计算向量化；必要时增加跳采样粗扫 + 精扫 | `4K_H264.mov` no-proxy benchmark 达到目标或自动提示 proxy |
| 6 | SC-REM-13 | `tools/benchmark_scene_cut_detection.py` | 增加 `--proxy-media` 参数和 1080p proxy target 识别 | benchmark 报告能输出 `1080p_proxy_8x_realtime=true/false` |
| 7 | SC-REM-14 | `tools/benchmark_scene_cut_detection.py` | all-fixtures 报表跳过 cache target；cache target 只看 `cache_hit=True` 的 media benchmark | fixture 报表不再显示误导性的 cache failure |

## 5. P2 实施步骤

| 步骤 | 对应问题 | 修改文件 | 具体做法 | 验证方法 |
|---|---|---|---|---|
| 1 | SC-REM-15 | `scene_cut_dialog.py` | 检测完成改为底部状态提示，不弹 modal；Add Cuts 保留确认弹窗 | 手动打开检测，完成后不阻塞继续审核 |
| 2 | SC-REM-16 | `tests/edit/` | 新增右键入口到媒体池 subclip 的集成测试；mock QMessageBox 返回 Yes | 测试断言 project.assets 新增 subclip，父素材保持不变 |
| 3 | SC-REM-17 | `scene_cut_graph_widget.py`、`scene_cut_timeline_overlay.py`、`apply_ops.py` | 统一 warning/accepted/rejected 的颜色和 marker 状态 | UI snapshot 或 widget 单测断言 graph/list/overlay 状态一致 |

## 6. 推荐开发顺序

| 顺序 | 任务 | 原因 | 完成标志 |
|---|---|---|---|
| 1 | 修 subclip 上时间线 source range | 这是用户能直接感知的严重链路 bug | subclip 从媒体池拖入后播放母素材正确区间 |
| 2 | 修 UI 缓存保存和 proxy cache key | 会影响长素材重复打开体验，也避免 proxy/master 串结果 | dialog 第二次打开命中同一缓存，proxy/master cache 分离 |
| 3 | 收敛 4 个 xfail | 这是当前算法完成度最明确的红线 | `test_scene_cut_false_positive_guards.py` 无 xfail |
| 4 | 建 proxy/master 精确映射测试 | proxy 已接通，但必须证明不同 fps/VFR 不偏帧 | 12fps proxy 到 24fps master 映射通过 |
| 5 | 做异步预览加载 | 提升长素材审核手感 | 快速点选候选不卡顿、不串图 |
| 6 | 完成性能矩阵 | 决定是否默认提示/强烈推荐 proxy | 2K/4K/proxy/no-proxy 报表稳定 |

## 7. 验证命令

基础回归：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_scene_cut_models.py \
  tests/edit/test_scene_cut_detector.py \
  tests/edit/test_scene_cut_false_positive_guards.py \
  tests/edit/test_scene_cut_cache.py \
  tests/edit/test_scene_cut_timeline_mapping.py \
  tests/edit/test_scene_cut_subclips.py \
  tests/edit/test_scene_cut_project_serialization.py \
  tests/edit/test_scene_cut_result_model.py \
  tests/edit/test_scene_cut_worker.py \
  tests/edit/test_scene_cut_apply_ops.py \
  tests/edit/test_scene_cut_vfr_mapping.py
```

查看剩余 xfail：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q -rx \
  tests/edit/test_scene_cut_false_positive_guards.py
```

Fixture benchmark：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --all-fixtures --json
```

真实素材 benchmark：

```bash
.venv/bin/python tools/benchmark_scene_cut_detection.py --media "tests/test_videos/2K_h264.mp4" --json
.venv/bin/python tools/benchmark_scene_cut_detection.py --media "tests/test_videos/2K_prores422HQ.mov" --json
.venv/bin/python tools/benchmark_scene_cut_detection.py --media "tests/test_videos/2K_H265.mov" --json
.venv/bin/python tools/benchmark_scene_cut_detection.py --media "tests/test_videos/4K_H264.mov" --json
```

## 8. 最终完成标准

| 类别 | 标准 |
|---|---|
| 测试 | 场景剪切 targeted 回归无失败、无 xfail |
| 预览 | 三联预览异步加载，不串图，不阻塞审核 |
| 检测 | hard cut、dissolve、black gap、flash、fast pan、VFR fixture 全部达到验收 |
| 缓存 | UI 检测完成会保存；第二次打开命中；proxy/master cache 不串 |
| proxy | proxy 检测结果准确映射回 master frame，支持不同 fps/VFR 验证 |
| 媒体池 | Add Cuts 后 subclip 元数据正确，拖入时间线播放母素材正确区间 |
| 性能 | 2K/4K/proxy/no-proxy 报表可信；长素材内存增长低于 300MB |
| 体验 | 完成检测不弹阻塞提示；疑似误判高亮清晰；接受/拒绝/Add Cuts 状态一致 |

