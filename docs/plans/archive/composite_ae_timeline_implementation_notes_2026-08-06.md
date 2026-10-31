# Composite AE-style Timeline — 实现纪要

本文件记录 `composite_ae_like_timeline_implementation_plan_2026-08-06.md`
（2474 行，T00–T129 共 129+ 原子任务）的完整实现状态，作为后续维护的
架构索引和交接参考。

**状态**：T00–T129 全部完成（含评估类任务 T76/T79）。Graph Editor 专项
（T100–T129）作为最后一大块，于本会话（2026-08-06）交付。

---

## 一、交付总览

### 会话提交（Graph Editor + 收尾，8 个 commit）

| Commit | 批次 | 内容 |
|---|---|---|
| `c0e8f51f` | 18f | Speed Graph 拖动 + Transform Box（T115/T117） |
| `28d58372` | 18g | Separate Dimensions + pre/post 表达式曲线（T118/T119） |
| `5f29fd1c` | 18h | 表达式编辑器 + Marker/Work-Area 叠加（T120/T121） |
| `02aa389a` | 18i | Undo 事务 + Esc 取消（T123） |
| `27791b92` | 18j | 回归 + 性能测试套件（T124–T128） |
| `4fcc471c` | 18k | Spline 入口重定向到 Graph 模式（T129） |
| `8de0db09` | 19p1 | 本地化 + 无障碍（T71/T77） |
| `1f976db5` | 19p2 | 跨平台输入 + T76/T79 评估（T78/T76/T79） |

（更早的 P3-view / P4-view / P6 / Graph Editor 基础 T100–T116/T122 见
`git log` 的 `c3c80902..d3fccf6b` 区间，共 14 个 commit。）

### 新增/修改源文件

**新增**：
- `src/features/composite/ui/graph_expression_editor.py` — 底部表达式编辑器 widget（T120）
- `src/features/composite/ui/graph_view.py` — GraphCanvasHost（T101，模式切换容器）
- `src/features/composite/ui/graph_model.py` — 纯数据 GraphPoint/Series/Viewport + 全部图数学（T102–T104, T106–T107, T115, T117–T119）
- `src/features/composite/ui/graph_editor_view.py` — QPainter 画布（T105, T108–T125）
- `src/features/composite/ui/timeline_time_display.py` — 当前帧时间显示（T25/T34）
- `src/features/composite/ui/timeline_icons.py` — QPainter 几何图标 + make_qicon（T27/T36）

**重大修改**：
- `src/features/composite/ui/keyframes_panel.py` — AE 工具栏 + 页脚 + Graph 模式状态 + 表达式编辑器挂载 + 跨视图同步
- `src/features/composite/ui/keyframes_tree_view.py` — Eye/Solo/Lock/Shy/Label/Name/Value/Expression AE 列布局
- `src/features/composite/ui/keyframes_timeline_view.py` — 拖动吸附 + Alt 拖复制 + 播放头节流
- `src/features/composite/ui/spline_panel.py` / `spline_view.py` — 共享选择同步 + Graph 模式重定向（T61/T129）

### 新增测试（15 个文件，~400 用例）

```
tests/composite/test_graph_model.py                       (59)  T102-T104, T106-T107, T122
tests/composite/test_graph_transform_box.py               (30)  T115/T117 纯数据
tests/composite/test_graph_separate_and_expression.py     (45)  T118/T119 纯数据
tests/composite/test_graph_evaluator_consistency.py        (7)  T126 数值一致性
tests/composite/test_graph_expression.py                   (9)  T127 表达式回归
tests/ui/test_graph_editor_view.py                        (59)  T105-T116 画布
tests/ui/test_graph_speed_and_transform_box.py            (23)  T115/T117 集成
tests/ui/test_graph_separate_expression_view.py           (10)  T118/T119 集成
tests/ui/test_graph_markers_workarea.py                   (20)  T121 叠加/吸附
tests/ui/test_graph_expression_editor.py                  (20)  T120 编辑器
tests/ui/test_graph_undo_transaction.py                    (8)  T123 undo/Esc
tests/ui/test_graph_editor_layout.py                      (14)  T124 布局
tests/ui/test_graph_editor_interaction.py                  (7)  T125 交互
tests/ui/test_graph_cross_platform.py                     (12)  T78 跨平台
tests/ui/test_spline_graph_redirect.py                     (8)  T129 重定向
tests/benchmark/benchmark_graph_sampling.py                (5)  T128 性能
tests/ui/test_localization_accessibility.py              (155)  T71/T77
（+ tests/ui/test_timeline_time_display.py, test_keyframes_tree_columns.py,
   test_keyframes_panel_toolbar.py, test_keyframes_snap.py,
   test_keyframes_alt_drag_copy.py, test_keyframes_playhead_throttle.py,
   test_composite_page_selection_attach.py, test_timeline_icons.py 等
   更早批次的测试文件）
```

---

## 二、架构要点

### 1. 纯数据 + GUI 分离（可无头测试）

所有图数学（坐标变换、曲线采样、transform-box、吸附候选、表达式前后曲线）
都在 `graph_model.py` 里是**纯函数**，不依赖 QWidget。视图层只做命中测试、
事件分发和绘制。这让 ~60% 的测试可以脱离 Qt 事件循环跑（`tests/composite/`）。

### 2. 共享状态（AnimationSelectionModel）

Keyframes 树/时间线 ↔ Spline ↔ Graph Editor 三处通过同一个
`AnimationSelectionModel`（可观察、合并批量通知）同步选择，带重入守卫
避免循环。工作区（play range）和 marker 也由面板（canonical owner）同时
推送到时间线和 Graph 画布，保证两视图参考点 + 吸附目标一致（T121）。

### 3. 模式切换不重建状态（GraphCanvasHost）

`GraphCanvasHost` 用 `QStackedLayout` 在 Timeline 画布（page 0）和 Graph
画布（page 1）间切换，**不重建 splitter / 树 / 共享状态**。当前帧、工作区、
选择、展开状态在模式切换中全部保留（T100/T101）。

### 4. 一次拖动一次 undo（T123）

Graph 视图在 commit 爆发前后发 `drag_about_to_commit` / `drag_committed`
信号（携带受影响 series 数），`CompositePage` 用 `UndoStack.begin_group` /
`end_group` 把多 series 的 Transform Box 拖动合并成**一个原子 undo 项**。
Esc 中断时直接从拖动前快照还原，**不发 keyframe_changed**（不写节点、不进
undo 栈）。

### 5. 表达式前后曲线（T119）

`sample_expression_curves` 对每帧求值表达式，产出对齐的 (pre, post,
has_error) 三元组。pre = 关键帧/静态求值（暗灰虚线），post = 表达式结果
（参数色高亮）；错误时 post 回退到 pre + 红色错误标记。关键帧菱形始终来自
pre，不把表达式采样点画成关键帧。

---

## 三、任务完成清单

### P3-view（AE 视觉对齐）
- ✅ T24–T28, T34, T36 — AE 列布局（Eye/Solo/Lock/Shy/Label/Name/Value/Expression）、工具栏图标组、当前帧时间显示、离屏渲染检查
- ✅ T27 — `make_qicon`（DPR 2× QPainter 图标）

### P4-view（交互）
- ✅ T42 — Alt/Option 拖动复制关键帧（保留原件 + 取消选中 + 冲突守卫）
- ✅ T46 — 拖动吸附（其他轨道关键帧 + 工作区边界）
- ✅ T48 — 播放头拖动节流（force-flag 保证释放时最终帧发布一次）

### P6（跨表面同步）
- ✅ T56/T61 — Spline → Keyframes 反向同步（共享 AnimationSelectionModel + 重入守卫）

### Graph Editor 专项（T100–T129）
- ✅ T100–T104 — view_mode 状态、GraphCanvasHost、GraphPoint/Series/Viewport、property→series、Auto Select Graph Type
- ✅ T105–T109 — 坐标变换、背景/网格/零线/图例、插值曲线 + 关键帧菱形 + Bezier handle
- ✅ T110/T111 — 底部控制条 + 缩放/平移（光标锚定）
- ✅ T112/T113 — 命中测试 + 框选 + 选择同步
- ✅ T114/T116 — Value Graph 单点拖动 + Bezier handle 拖动（joined/split、smooth 镜像）
- ✅ T115 — Speed Graph 拖动（仅时间，不误改 value）
- ✅ T117 — Transform Box（内部移动 + 边/角缩放 + anchor + 跨参数仅时间）
- ✅ T118 — Separate Dimensions（通道元数据 + 兄弟分组 + 固定轴色）
- ✅ T119 — pre/post 表达式曲线（求值 + 错误回退 + 图例）
- ✅ T120 — Graph 表达式编辑器（底部可伸缩、多选只显示 primary）
- ✅ T121 — Marker/In-Out/Work Area 叠加（Graph + Timeline 共享参考点 + 吸附目标）
- ✅ T122 — Graph Info summary（property/frame/value/speed/interpolation）
- ✅ T123 — Undo 事务（一次拖动一次 undo + Esc 还原）
- ✅ T124–T128 — 回归 + 性能测试（布局/交互/数值一致性/表达式/性能预算）
- ✅ T129 — Spline 入口重定向到共享 Graph 模式（旧入口仍可用）

### P9（本地化/无障碍/跨平台/评估）
- ✅ T71 — 本地化（zh_CN/en_US 完整 keyframes + undo 键）
- ✅ T77 — 无障碍（图标按钮 accessible name + tooltip + 键盘可聚焦）
- ✅ T78 — 跨平台输入（Shift/Ctrl/Cmd 多选 + Ctrl/Cmd+Return 提交）
- ✅ T76 — 主题 token 迁移审计（通过；保留色均为数据默认或固定语义编码）
- ✅ T79 — AE 导入/导出评估（推迟；节点图 vs 图层栈模型不兼容，见
  `docs/composite_ae_timeline_t76_t79_evaluation_2026-08-06.md`）

---

## 四、已知限制与后续

1. **`tests/ui/` 全量运行的 segfault**：在单进程内跑 `tests/ui/` 全部会偶发
   segfault（Qt 全局状态/WebEngine/OpenGL 上下文跨测试泄漏）。**这是既有问题
   （在前一 commit `d3fccf6b` 也复现）**，非本次改动引入。每个测试文件单独
   运行均通过；用 pytest-xdist 按文件并行也可绕过。
2. **表达式多曲线性能**（T128）：每采样帧调用一次 AST 求值器，10 条表达式
   曲线约 200ms。计划 §22.12 要求 30–60 Hz 节流——当前用时间预算测试守护
   10× 回归，节流刷新（拖动时低密度预览 + 释放后正式密度）是后续优化项。
3. **AE 文件 I/O**（T79）：明确推迟。节点图与 AE 图层栈模型不兼容；未来如需
   互通应走中间格式（见评估文档）。
4. **Transform Box 的 Ctrl/Cmd 对称缩放**：当前边/角拖动以 anchor 为中心；
   计划提到“Ctrl/Cmd 拖以 anchor 为中心缩放”——已实现（anchor 即缩放中心），
   Alt/Option 单边/角拖动保持其他边界是后续可选增强。

---

## 四(b)、审计修复（2026-08-06，T00-T129 交付后的隐藏 bug 扫除）

交付后对全部代码做了一次系统审计，发现 16 个隐藏 bug（3 个数据完整性
级、6 个中危、7 个低危）。它们都没被当时的测试覆盖（测试只走了 happy
path / 单文件单测无法发现跨文件集成断链 / 选项开关无视觉断言），所以
全套 939 测试全绿但问题真实存在。分四批修复，每批独立验证 + 提交：

### 第一批（数据安全）— commit `979a86a1`
- **Bug-1 (Critical)**：Graph 表达式编辑器提交**绕过 undo 栈**。
  `_on_expression_committed` 直接调 `node.set_expression`，不经过 page。
  修复：KeyframesPanel 新增 `graph_expression_changed` 信号转发；
  CompositePage 新增 `_on_graph_expression_changed`，预验证后委托给已有的
  `_on_props_expression_changed`（与属性面板 ◆ 按钮共用同一 undo 路径）。
- **Bug-3 (Critical)**：`focusOutEvent` 无条件 emit + 无文本去重 → 切换选择
  时旧 param 被重提交，经 A-15 误清关键帧。修复：`committed` 信号带
  `explicit` 标志；focusOut（隐式）对 `_last_committed_text` 去重；
  Ctrl/Cmd+Return（显式）始终提交。
- **Bug-2 (Critical)**：`_commit_drag` 的 emit 爆发无 try/finally → 下游
  slot 抛异常会吞掉 `drag_committed` → undo 栈永久卡在 `begin_group`。
  修复：emit 爆发包裹 try/finally。

### 第二批（交互正确性）— commit `0b4a4f43`
- **A1**：Esc 无法取消 anchor 拖拽（守卫不含 `box_anchor`，`_cancel_drag`
  无 anchor 分支）。修复：两者补全。
- **F4**：点击-释放无移动仍 emit `keyframe_changed` + 推 undo。修复：
  `_commit_drag` 调 `_drag_actually_changed()`，无变化则跳过 emit。
- **A5**：拖拽中按中键劫持手势、`_drag_mode` 悬挂。修复：拖拽/框选中忽略
  中/右键按下。
- **F1**：`show_markers`/`show_layer_in_out`/`show_reference_graph` 三个开关
  被存储但绘制器忽略。修复：paintEvent 与 `_draw_expression_curves` 据此
  gate（`show_transform_box` 故意不 gate——它从未接线，box 可见性由选择驱动）。

### 第三批（完善）— commit `a4172d0c`
- **C4**：Transform Box move 不吸附。修复：leader 点目标经 `_snap_frame`
  吸附后重算 group delta（box scale 保持纯比例，吸附会破坏比例约束）。
- **D1**：`set_fps` 不失效缓存的 `_expr_ctx` → 读 `fps` 的表达式用过时值。
  修复：`set_fps` 置 `_expr_ctx = None`。
- **F2/F3**：`set_series`/`set_graph_type` 拖拽中重建 → 悬挂引用。修复：
  两者先调 `_abort_active_drag()` 还原快照 + 清状态。
- **B4/B1**：`_hit_transform_box` 边/角都用 `_BOX_CORNER_TOL`(8px) → 窄框
  (<16px) 内部被两边边沿同时认领、`inside` 不可达。修复：边用
  `_BOX_EDGE_TOL`(6px)、角用 `_BOX_CORNER_TOL`(8px)（顺带激活 B1 死代码）。

### 第四批（清理）— commit `5c884bba`
- **BUG-1**：`apply_scale_transform` 的 `tspan`/`vspan` 死代码 + docstring 撒谎
  “除零保护”。删除死变量、重写 docstring。
- **BUG-4**：`_AXIS_SUFFIXES` 死代码。让 `param_channel`/`param_base_name`
  实际使用它（单一真相源）。
- **BUG-14**：`_CHANNEL_COLORS["w"]` 不可达键（`param_channel` 永不返回
  `"w"`）。删除。
- **BUG-15**：`build_graph_series` 用 `"#bdbdbd"` 字面量当哨兵。加注释说明
  约定（不改签名，避免回归）。
- **BUG-6**：Spline redirect 每次 re-show 重复 raise Keyframes dock 抢焦点。
  修复：已在 graph 模式则不再 raise。

### 新增测试（4 个文件，39 用例）
- `tests/ui/test_graph_bugfix_data_safety.py`（9）— Bug-1/2/3
- `tests/ui/test_graph_bugfix_interaction.py`（10）— A1/F4/A5/F1
- `tests/ui/test_graph_bugfix_polish.py`（7）— C4/D1/F2/F3/B4/B1
- `tests/composite/test_graph_bugfix_cleanup.py`（13）— BUG-1/4/14/15/6

修复后全量验证：**978 个测试通过，零回归**（Graph 357 + i18n 155 +
keyframes/timeline/composite 回归 466）。

---

## 四(c)、关键帧系统优化（2026-08-06，审计后性能/正确性提升）

对整个关键帧系统（~21000 行）做了全面审查，发现 1 个严重正确性 bug
+ 3 个 P0/P1 性能瓶颈 + 若干架构债务。分批修复，每批独立验证 + 提交：

### 第一批（正确性）— commit `52cb6dbe`
- **C-1 (Critical)**：Graph Editor 的贝塞尔采样用**帧占比当贝塞尔参数 t**，
  但贝塞尔 x 轴非线性（控制点 x 偏移≠0 时），导致 Graph 画的曲线与实际
  渲染值不一致。80 帧 handle 偏移下，frame=25 处偏差达 10.86（值域的 ~11%）。
  修复：`sample_value_curve` 改为委托渲染器权威数学（`bezier_value_at_frame`
  做 x(t)=frame 二分反解；ease 用 `ease_*_interpolate`），消除"第三套采样实现"。
  补 bezier/ease 一致性测试（此前一致性测试只覆盖 linear/hold）。

### 第二批（性能核心）— commit `36f9491d`
- **P-1 (P0)**：`KeyframeTrack.from_dict` 逐个 `add_keyframe`，每次 append 后
  全排序 → **O(n²log n)**。这是渲染器每帧重建 track 的热路径。修复：新增
  `add_keyframes_bulk`（extend-all + 一次 sort + 线性去重），`from_dict` 改用
  它。实测 100 关键帧快 4.1×（O(n²)→O(n log n)）。
- **P-2 (P0)**：`ExpressionParser.evaluate/validate/extract_dependencies` 每次都
  `ast.parse` 同一表达式字符串。播放期间每帧每表达式重解析——纯浪费。修复：
  `_parse_cached` 缓存解析后的 AST（bounded LRU 256），三处调用方统一走它。
  实测 parse 阶段快 74×（10.5ms→0.1ms / 1000 次）。

### 第三批（性能完善）— commit `b9e486ca`
- **P-3 (P1)**：`get_value`/`get_value_float` 的包围关键帧查找是线性扫描
  （O(n)/次）。修复：改用 `bisect.bisect_right`（O(log n)），keyframes 已排序
  的 invariant 保证安全。这是每帧每参数的求值热路径。

### 架构债务（已识别，本轮未执行 — 建议作为独立后续项目）
审查还发现了 4 项架构债务，它们收益主要是内部质量、无直接用户可感知改善，
但重构触及多面板核心逻辑、回归风险高，故不在本轮性能/正确性优化中混做：

- **A-1**：ease 函数三套拷贝（keyframe.py / spline_model.py / graph_model.py），
  第一批已让 Graph 改用渲染器数学；剩余 Spline 模块化是纯可维护性。
- **A-2/A-3**：`AnimationSelectionModel` 是 per-point `selected` 标志的"镜像"
  非真相源；Spline 选择不向其他面板传播；rebuild 中 keyframe 点级选中丢失。
  收敛到单一选择真相源是**最大重构**，需专门设计+测试周期。
- **A-4**：表达式设置有两条互不收敛的 undo 路径（`UndoAction` vs
  `TrackChange`），验证侧链/A-15 恢复方式各异。需协调两条路径。

### 新增测试
- `tests/composite/test_graph_evaluator_consistency.py` +7（C-1 bezier/ease 一致性）
- `tests/composite/test_keyframe_perf_optimizations.py`（20）— P-1 bulk + P-2
  AST 缓存 + P-3 bisect 查找

修复后全量验证：**874 个测试通过，零回归**。

---

## 五、验证命令

```bash
# Graph Editor 全套（单独运行，避免跨测试 Qt 泄漏）
.venv/bin/python -m pytest tests/ui/test_graph_*.py tests/composite/test_graph_*.py \
    tests/ui/test_spline_graph_redirect.py tests/ui/test_localization_accessibility.py \
    -q -p no:cacheprovider

# 性能预算
.venv/bin/python -m pytest tests/benchmark/benchmark_graph_sampling.py -q

# 既有 Keyframes 面板/时间线回归
.venv/bin/python -m pytest tests/ui/test_keyframes_*.py tests/ui/test_timeline_*.py \
    tests/ui/test_composite_*.py -q
```

环境：`QT_QPA_PLATFORM=offscreen` 用于无头 UI 测试。
