# S00/S01 示波器复现与分段诊断记录（2026-11-02）

本文件是计划 S00–S02 的实施记录与接续点。**诚实边界先行**：

- S01（分段诊断）已实施并有 fake-demand 测试证据（22 项）；
- S00 的完整原生复现矩阵（测试素材01.mov × 全模式 × 暂停/播放/seek/
  切 clip/隐藏再打开 × 停靠/浮动 × 窗口尺寸）与 S02 的原生修复验证
  **NOT RUN**——需要原生 GUI 会话逐步操作并截图，本轮会话无法替代，
  不以 offscreen 回归替代宣称完成。

## S00 已有现场事实

- 计划文档现场检查（2026-11-02）：原生窗口测试素材主画面与 Waveform
  均有图像输出；只证明单波形有输出，不证明全部模式/刷新/数值/共享
  纹理生命周期正常；用户报告的异常尚未定位根因。
- offscreen 回归：现有链路 125 项通过（test_scope_live_refresh /
  test_scope_input_source / test_scope_multiview_ui /
  test_gpu_scope_pipeline），本轮复跑仍全绿。
- 代码层审查（B00 调用图 + 本轮 Explore 报告）发现的**静默丢弃面**：
  E09 提交侧 6 类、E02 转发 1 类、E03 队列/执行 8 类、面板接收 3 类、
  呈现 4 类——全部无计数（仅 pass_exception 有去重日志）。这正是
  "黑屏难定位"的结构性原因，属 S01 范畴已修复（见下）。

## S01 实施（已完成）

新模块 `src/features/color/core/scope_diagnostics.py`：

- `(stage, reason)` 计数 + 每键最后上下文（revision/source_revision/
  mode/tex_id/尺寸白名单，不持有纹理/帧对象）；
- 可关闭：`TOPOS_SCOPE_DIAG=0`（或 `set_enabled(False)`）；
  `verbose` 档按键去重告警（每键 ≤3 次，与 `_gpu_scope_errors` 同规约）；
- 线程安全（UI 线程 + 渲染线程共用；锁内纯 dict 操作）；
- 进程级单例 `scope_diagnostics.snapshot()`；渲染统计
  `get_render_stats()["gpu_scope_diagnostics"]` 内嵌同快照。

接线点（每处原先都是静默 return）：

| 段 | 位置 | 原因码 |
|---|---|---|
| submit | E09 `_submit_gpu_scope_demand` | `no_renderer` `panel_hidden` `gpu_lane_disabled` `panel_missing` `curves_dedup_skip` `rate_limited` `submit_exception` |
| submit | E09 `on_gpu_scope_ready` | `not_visible_on_return` `stale_return` `present_exception` |
| queue | E02 `request_gpu_scope` | `no_renderer` |
| queue | E03 `submit`（latest-wins） | `coalesced_replaced` |
| execute | E03 `_do_render_gpu_scope` | `gpu_lane_disabled` `demand_shape_invalid` `no_front_texture` `shaders_not_loaded` `stale_revision` `no_render_params` `color_output_failed` `output_texture_invalid` `pass_exception` |
| panel | `ScopePanel.update_gpu_scope` | `frame_none` `stale_revision` `source_not_graded` |
| widget | `GpuScopeWidget.paintGL`/`_blit_shared_texture` | `mutex_busy` `blit_failed` `texture_not_shared` `fbo_incomplete` |

验收对应：`tests/color/test_scope_diagnostics.py`（22 项）——每类拒绝
原因用 fake demand/渲染器/面板/部件触发并断言计数与上下文；诊断可
关闭；快照白名单；并发计数正确性；stats 内嵌。

## 原生运行时如何使用（S00 复现步骤配套）

1. 终端启动：`TOPOS_SCOPE_DIAG=1 .venv/bin/python -m src.app.main`
   （或默认开；排障时可加 `verbose` 档看去重告警）。
2. 复现异常（按 S00 协议记录：模式/播放状态/输入源/帧号/操作）。
3. 读取事实：任何一处 Python 控制台/日志执行
   `from src.features.color.core.scope_diagnostics import scope_diagnostics;`
   `scope_diagnostics.snapshot()`——直接回答"未请求（submit 段）/未执行
   （queue/execute 段）/回传被丢弃（panel 段）/呈现失败（widget 段）"。
4. `reference 无 still 时为空不是 GPU 故障`（S00 注记）：`source_not_graded`
   计数与 source/reference 语义相关，勿据此判 GPU 故障。

## S02 状态（未修——无命中证据）

按计划"按证据只修命中的层"：offscreen 全绿 + 本轮未获得原生异常的
分段证据，**没有可命中的层**，不做臆测性修改。已知并已转为可诊断的
风险点：shader 编译失败会永久关闭 GPU lane（原为静默；现
`execute/shaders_not_loaded` 计数 + submit 侧 `gpu_lane_disabled`
联动可见）。待 S00 原生证据到位后，按 snapshot 指向的层做 focused
修复，并按 S02 验收补原生矩阵（waveform/parade/luma/vectorscope/
histogram/cie/falsecolor、双/四/全部模式、切换/浮动/resize）。

## 回归命令

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_scope_diagnostics.py \
  tests/color/test_scope_live_refresh.py tests/color/test_scope_input_source.py \
  tests/color/test_scope_multiview_ui.py tests/color/test_gpu_scope_pipeline.py -q
# 147 passed（125 既有 + 22 新增）
```
