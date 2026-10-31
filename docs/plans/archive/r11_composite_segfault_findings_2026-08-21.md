# R11 调查结论：tests/composite 段错误定位（2026-08-21）

状态：**已定位触发条件与隔离方案；原生根因待 ASan 级调查。**

## 1. 症状

`pytest tests/composite/`（目录形式，单进程）在约 21% 进度处稳定崩溃：

```
Fatal Python error: Segmentation fault
Current thread ...:
  pytestqt/plugin.py:220 in _process_events
  pytestqt/plugin.py:202 in pytest_runtest_teardown
```

## 2. 已证实的结论

1. **不是测试失败导致的连锁**：修复全部陈旧断言后（见 §4），崩溃依旧。
2. **崩溃点漂移但聚集**：多次运行分别落在
   `test_composite_graph_render_service_contract.py::TestCompositeGraphRenderServiceCpu`
   的 teardown、`test_composite_pixel_level.py` 前 4 个测试之后，以及（旧提交上）
   `_format_failed_longrepr` 期间的 Bus error。崩溃点不固定 → 典型的
   use-after-free / 堆损坏在事件循环里的随机显形。
3. **需要大前缀 + 特定文件组合**：
   - 前 38 个文件（964 tests）显式列出运行 → 通过。
   - 前 50 个文件 + `test_composite_pixel_level.py` → 崩溃。
   - 前 36 个 + 该文件 → 通过；说明是累积阈值效应而非单一毒文件。
   - 只含 pixel_level 导入语句的探针模块 + 前 38 个文件 → 通过；
     **导入本身无罪**，需要 pixel_level 的测试体与其他大量测试共存。
4. **早于 MG 分支存在**：在 2026-08-19 之前的提交（207d08b6）上同样崩溃
   （Bus error 变体），非 `fix-ui`/MG 工作引入。
5. **测试体全为纯 Python/mock 的测试也会在 teardown 崩**：
   `test_render_cpu_returns_numpy` 全 mock、无 GL 调用 → 崩溃是"错误的时机"
   而非"错误的代码"：之前某个测试留下的失效原生对象（deferred delete /
   timer / GL 上下文）在 processEvents 时被触碰。

## 3. 隔离方案（已落地）

`scripts/run_composite_tests.sh`：按文件分进程运行整套 tests/composite
（含 test_nodes/ 子目录），进程边界天然阻断跨文件的堆损坏传播。在该脚本下
全套 169 个文件全部通过。

单进程根因修复前的项目报告口径：**MG 通过、composite 单进程全量未通过
（进程隔离下全量通过）**。

## 4. 同批修复的陈旧测试（段错误掩盖的真实失败）

以下 18 个失败在段错误修复前从未被执行到，全部为测试陈旧（对应功能已按
设计演进），非产品回归：

| 测试 | 陈旧原因 |
|---|---|
| `test_3d_phase6_components_snap.py::test_point_edge_polygon_disabled` | P7-5（44fbb110）已交付面级 picking，Polygon 启用 |
| `test_global_3d_scene_settings.py::test_bumps_schema_version_to_9` | schema 已是 10；新增 v9→v10 迁移测试 |
| `test_composite_graph.py::test_complex_graph_roundtrip` | babcd9aa 起独立图像图跳过 color_input 迁移；补迁移路径测试 |
| `test_pivot_display.py`（2 个） | combo 现为翻译 display 标签 + index 映射，setCurrentText(原始键) 无效 |
| `test_node_properties_panel_xy.py`（2 个） | 9b9be8fa 面板紧凑化：XY pair 行 slider 注册到 `_pair_sliders`；single slider 恒显 |
| `test_sticky_note.py`（6 个） | 标题栏已移除；padding 8→10（ee007c43） |
| `test_topos_clickable_slider_velocity.py`（4 个） | e3df12fe 拖拽 base 改为宽度自适应（/widget_width） |
| `test_quad_view_quality.py`（1 个） | quad_active_view 改为防御性 getattr 形式 |
| `test_scene3d_quality_profile.py`（1 个） | nav 标志组合移入 `_view_signature_non_cam` |
| `test_composite_pixel_level.py::test_mask_node_all_types` | `circle` 为用户别名，内部规范化为 `ellipse`（aff47af3） |

## 5. 隔离脚本运行中新发现（2026-08-22 补充）

- `tests/composite/test_keyframes_composition_range.py` 在 pytest 下第一个
  测试 `test_keyframes_panel_starts_with_full_composition_work_area` 挂起
  （单文件 60s 超时未完成；烧满一个 CPU 核）。脱离 pytest 直接构造
  `KeyframesPanel()` + `set_frame_range()` 正常，怀疑 pytest-qt 事件循环与
  面板内某个信号/timer 交互死锁。**未修复**——需要在 R11 根因调查中一并
  处理；在那之前跑该文件请加超时。
- `tests/core/` 与两个并行全量 pytest 进程同时跑时显著变慢（CPU 争抢），
  独立运行 2.6s 完成。跑全量回归时避免并行。

## 6. 后续（根因调查建议）

- 用 AddressSanitizer 构建 PyQt6 不可行时，可用 `MallocStackLogging` +
  `malloc_history`，或逐步二分"前缀文件集合 × pixel_level"找到留下失效原生
  对象的测试（候选方向：ModernGL 离屏上下文、QtWebEngine 预导入、
  `composite_page` 生命周期中的 deleteLater 时序）。
- pytest-xdist（`-n auto`）是另一个进程隔离选项，但会改变测试并发语义，
  需单独验证 fixture 兼容性后再引入。
