# AI 完成内容复核与剩余优化计划（2026-05-27）

> 目的：复核上一轮 AI 声称完成的 Phase 0-13 内容，明确哪些已经可信、哪些存在偏差，并给出下一轮可执行的剩余优化计划。
>
> 当前结论：大部分模块化后续修复确实完成，而且比原报告更扎实；但仍有三个关键问题必须继续处理：
>
> 1. `src/app/main_window.py` 仍有 9216 行，Phase 5 没做，是最大剩余架构债。
> 2. `tests/visual` 单独运行仍会 segfault，不能算 Phase 11/Visual 完全完成。
> 3. 测试矩阵文档和 pytest marker 没有真正打通，`-m color_core/composite_core/media_core` 当前会 0 selected。

---

## 1. 本次复核摘要

### 1.1 Git 状态

复核时工作区干净：

```bash
git status --short
```

结果：无未提交变更。

最近 10 个相关 commit 存在：

```text
6d0a0b7 fix: repair sticky note, backdrop, playback HUD tests (Phase 11)
2347b59 fix: repair export/ffmpeg/save router tests (Phase 9)
29a1d38 feat: add feature flag system and CI acceptance matrix (Phase 12 + 13)
9ddd563 fix: migrate composite node/ROI/tile test imports (Phase 7)
5e87ff7 fix: migrate color tools test imports to new modular structure (Phase 8)
0a48dd4 fix: migrate all legacy import paths to new modular structure (Phase 3)
612c6c8 fix: disable update checker network in tests (Phase 2), remove dead config/luts (Phase 4)
39828b6 docs: add test acceptance matrix (Phase 1) and harden shared boundary (Phase 6)
956f303 fix: stabilize modularized import boundaries and add optimization plan
3b3b02c refactor: remove legacy src/core and src/ui packages
```

### 1.2 `src` 目录状态

Git 跟踪的源码中，以下旧目录已经不存在：

```text
src/core
src/ui
src/composite
src/config.json
src/luts
```

当前 Git 跟踪源码顶层目标基本达成：

```text
src/
  __init__.py
  app/
  features/
  main.py
  shared/
```

注意：本地运行仍可能生成 ignored 目录：

```text
src/.images_cache
src/__pycache__
```

这不算源码结构回退，但后续可以进一步把运行缓存彻底移出 `src`。

### 1.3 已验证通过的测试

#### Compile

```bash
.venv/bin/python -m compileall -q src tests
```

结果：通过。

#### Boundary / Feature Flags

```bash
.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_modular_import_boundaries.py \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  -q
```

结果：

```text
63 passed
```

#### 基础页面 / Color / Composite smoke

```bash
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  -q
```

结果：

```text
137 passed, 1 warning
```

#### Benchmark

```bash
.venv/bin/python -m pytest \
  tests/benchmark/test_latency.py \
  tests/benchmark/test_playback_performance.py \
  tests/benchmark/test_renderer_queue.py \
  tests/benchmark/test_ui_tick.py \
  -q
```

结果：

```text
16 passed
```

#### Composite 大范围逻辑测试

命令：

```bash
.venv/bin/python -m pytest \
  tests/test_composite_*.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  tests/test_dirty_propagation.py \
  tests/test_deep_compositing.py \
  tests/test_group_node.py \
  tests/test_node_capabilities.py \
  tests/test_node_graph_commands.py \
  tests/test_backdrop.py \
  tests/test_sticky_note.py \
  tests/test_parallel_evaluation.py \
  tests/test_render_scheduler.py \
  -q
```

结果：

```text
1325 passed, 85 warnings
```

说明：AI 汇报的 “Phase 7: 521 passed” 数字偏小；当前更宽范围已全绿。

#### Color tools / scripting

命令：

```bash
.venv/bin/python -m pytest \
  tests/test_color_tools_lut_exporter.py \
  tests/test_color_tools_params.py \
  tests/test_color_tools_scripting_api.py \
  tests/test_image_engine_params.py \
  tests/test_color_session.py \
  -q
```

结果：

```text
97 passed, 4 skipped
```

说明：AI 汇报 “120 passed” 与当前复核数字不一致，但该范围是绿的。

#### Export / FFmpeg / Save Router

```bash
.venv/bin/python -m pytest \
  tests/test_export_frame_provider.py \
  tests/test_ffmpeg_encoder.py \
  tests/test_save_router.py \
  -q
```

结果：

```text
173 passed
```

#### Sticky note / Backdrop / HUD

```bash
.venv/bin/python -m pytest \
  tests/test_sticky_note.py \
  tests/test_backdrop.py \
  tests/test_playback_hud.py \
  -q
```

结果：

```text
89 passed
```

#### Media core subset

```bash
.venv/bin/python -m pytest \
  tests/media/test_decode_worker.py \
  tests/media/test_p1_1_disk_proxy_priority.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/media/test_video_frame_provider_service.py \
  tests/media/test_video_frame_queue.py \
  -q
```

结果：

```text
172 passed
```

#### YUV / GPU upload

```bash
.venv/bin/python -m pytest \
  tests/test_video_texture_pool.py \
  tests/test_video_upload_command.py \
  tests/test_yuv_chroma_sampling_pixels.py \
  tests/test_yuv_gpu_upload.py \
  tests/test_yuv_temp_texture_release.py \
  tests/test_yuv_upload_api.py \
  -q
```

结果：

```text
223 passed
```

说明：AI 汇报“Phase 10 未完成，25 failed，需要真实 OpenGL 上下文”在当前机器上不成立。当前环境里 Phase 10 相关测试已通过。

### 1.4 复核失败项

#### Visual 测试仍然 segfault

命令：

```bash
.venv/bin/python -m pytest tests/visual -q
```

结果：

```text
Fatal Python error: Segmentation fault
```

堆栈关键路径：

```text
tests/visual/test_edit_page_visual.py
  edit_window_1280 fixture
src/app/main_window.py
  MainWindow.__init__
  create_panels
src/features/color/ui/mixer_panel.py
  MixerPanel.__init__
  init_ui
  create_mixer_tab
```

这说明 Visual 并不是“只需要显示器”这么简单。它在当前环境下单独运行也会崩溃，需要继续修。

#### Marker 矩阵没有真正生效

以下命令都选中了 0 个测试：

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_alpha_system.py tests/test_cache_fix.py -q -m color_core
.venv/bin/python -m pytest tests/test_composite_graph.py tests/test_composite_preset.py tests/test_expression_parser.py -q -m composite_core
.venv/bin/python -m pytest tests/media/test_decode_worker.py tests/media/test_video_frame_provider_service.py -q -m media_core
```

结果示例：

```text
collected 57 items / 57 deselected / 0 selected
```

这意味着 `docs/test_acceptance_matrix_2026-05-27.md` 里的 marker gate 还只是文档形态，不是真正可执行的 CI gate。

#### Phase 5 未完成

```bash
wc -l src/app/main_window.py
```

结果：

```text
9216 src/app/main_window.py
```

并且 `src/app/main_window.py` 仍有大量 deep import：

```text
src.features.color.core.*
src.features.color.ui.*
src.features.edit.core.*
src.features.edit.ui.*
src.features.composite.core.*
src.features.composite.render.*
src.features.deliver.ui.*
```

这和 AI 汇报“Phase 5 未完成”一致，是下一阶段最大任务。

---

## 2. 剩余任务优先级

| 优先级 | 任务 | 原因 | 建议规模 |
|---|---|---|---|
| P0 | 修复测试矩阵 marker 失效 | 当前 CI 文档命令会 0 selected，容易产生假绿 | 小到中 |
| P0 | Visual segfault 隔离/修复 | 进程崩溃比普通失败严重 | 中 |
| P1 | 拆分 `main_window.py` | 9216 行 app 壳仍是最大架构债 | 大 |
| P1 | app deep import 收敛 | 功能禁用/删除演练仍不彻底 | 中到大 |
| P2 | 统一 feature flag 系统 | 当前有 `src/app/feature_flags.py` 和 `src/shared/feature_flags.py` 两套概念 | 中 |
| P2 | cache/runtime path 移出 `src` | `.images_cache` 仍会生成在源码目录 | 小 |
| P3 | 文档和验收数字校准 | 当前报告中的 120 passed、25 failed 等数字不再准确 | 小 |

---

## 3. 新的执行计划

## Phase A：修复测试矩阵 marker，使 CI gate 真正可执行

### 目标

让以下命令不再 0 selected：

```bash
.venv/bin/python -m pytest -m color_core
.venv/bin/python -m pytest -m composite_core
.venv/bin/python -m pytest -m media_core
.venv/bin/python -m pytest -m modular
```

### 当前问题

`pytest.ini` 注册了 markers，`docs/test_acceptance_matrix_2026-05-27.md` 也写了 `-m` 命令，但测试文件没有实际打 marker。

### 推荐修复方式

优先使用 `pytest_collection_modifyitems` 根据文件路径自动打 marker，避免给几百个测试函数逐个加 decorator。

建议新增或修改：

```text
tests/conftest.py
```

实现规则示例：

```python
from pathlib import Path


COLOR_CORE_FILES = {
    "tests/test_color_session.py",
    "tests/test_alpha_system.py",
    "tests/test_cache_fix.py",
    "tests/test_image_engine_state.py",
    "tests/test_image_engine_layers.py",
    "tests/test_image_engine_params.py",
    "tests/test_history.py",
    "tests/test_layer.py",
    "tests/test_mask_shape.py",
    "tests/test_mask_group.py",
    "tests/test_tile.py",
    "tests/test_tiling.py",
    "tests/test_image_engine_history.py",
    "tests/test_image_engine_transforms.py",
}


def pytest_collection_modifyitems(config, items):
    root = Path(config.rootpath)
    for item in items:
        rel = Path(item.fspath).resolve().relative_to(root).as_posix()
        if rel in COLOR_CORE_FILES:
            item.add_marker("color_core")
        if rel.startswith("tests/media/"):
            item.add_marker("media_core")
```

Composite 文件较多，可用：

```python
if rel.startswith("tests/test_composite_") or rel in COMPOSITE_EXTRA_FILES:
    item.add_marker("composite_core")
```

Visual：

```python
if rel.startswith("tests/visual/"):
    item.add_marker("visual")
```

Modular：

```python
if rel in {
    "tests/test_src_directory_boundaries.py",
    "tests/test_modular_import_boundaries.py",
    "tests/test_page_context.py",
    "tests/test_feature_flags.py",
    "tests/test_feature_flags_registry.py",
}:
    item.add_marker("modular")
```

### 验证命令

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_alpha_system.py tests/test_cache_fix.py -q -m color_core
.venv/bin/python -m pytest tests/test_composite_graph.py tests/test_composite_preset.py tests/test_expression_parser.py -q -m composite_core
.venv/bin/python -m pytest tests/media/test_decode_worker.py tests/media/test_video_frame_provider_service.py -q -m media_core
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_page_context.py -q -m modular
```

预期：

- 每条命令都必须 selected > 0。
- 不允许出现 `0 selected`。

### 完成标准

- `docs/test_acceptance_matrix_2026-05-27.md` 中所有 marker gate 都是真实可运行的。
- 可以新增一个测试验证 marker gate 本身，例如：

```text
tests/test_acceptance_markers.py
```

检查关键文件会被预期 marker 收集。

---

## Phase B：修复 Visual segfault，不允许测试进程崩溃

### 目标

让以下命令不再 segfault：

```bash
.venv/bin/python -m pytest tests/visual -q
```

普通失败可以接受，segfault 不可接受。

### 当前崩溃链路

```text
tests/visual/test_edit_page_visual.py::edit_window_1280
  -> MainWindow()
  -> MainWindow.create_panels()
  -> MixerPanel()
  -> MixerPanel.create_mixer_tab()
  -> segfault during GC / PyQt widget creation
```

### 风险判断

这不一定是 `MixerPanel` 本身逻辑 bug，更可能是：

1. `MainWindow()` 在 visual fixture 中一次性创建了过多 Color/GL/Qt 面板。
2. 某些面板或 icon/resource 创建在测试环境下触发 PyQt native 崩溃。
3. fixture teardown 不充分，前序测试遗留 Qt 对象。
4. visual 测试只想测 Edit 页面，却创建了完整 Color 工作区。

### 修复策略

#### B1. 先做最小隔离复现

新增临时/正式 smoke 测试：

```text
tests/visual/test_main_window_visual_bootstrap.py
```

分层测试：

```python
def test_qapp_only(qapp_visual): ...
def test_mixer_panel_only(qapp_visual): ...
def test_main_window_construct_only(qapp_visual): ...
def test_main_window_edit_page_only(qapp_visual): ...
```

命令：

```bash
.venv/bin/python -m pytest tests/visual/test_main_window_visual_bootstrap.py -vv
```

目的：确认是 `MixerPanel` 单独崩，还是 MainWindow 全量构造时崩。

#### B2. 为 MainWindow 增加测试/轻量启动模式

建议扩展：

```python
MainWindow(test_mode=True, startup_page="Edit")
```

或通过 app context：

```python
MainWindow(app_context=AppContext(test_mode=True, enabled_features=...))
```

测试模式要求：

- 不启动 update checker。
- 不启动后台视频解码线程。
- 不启动自动扫描/网络/AI model 初始化。
- Edit visual 测试不创建非必要 Color 面板。
- 需要 Color 面板的测试单独创建。

#### B3. 修改 visual fixture

当前：

```python
window = MainWindow()
```

目标：

```python
window = MainWindow(test_mode=True, startup_page="Edit")
```

如果暂时没有 test mode，可以先用 feature flags 禁用无关页面，但不要禁用 color 核心到影响 MainWindow 初始化。

#### B4. 强化 teardown

fixture teardown 应：

```python
window.close()
window.deleteLater()
qapp_visual.processEvents()
```

如果 MainWindow 有 renderer/video/update checker：

```python
window.shutdown_for_tests()
```

建议新增 `shutdown_for_tests()`，集中关闭：

- update checker thread
- video decoder
- async renderer
- timers
- pending Qt signals

### 验证命令

```bash
.venv/bin/python -m pytest tests/visual/test_edit_page_visual.py -q
.venv/bin/python -m pytest tests/visual -q
.venv/bin/python -m pytest tests/test_sticky_note.py tests/test_backdrop.py tests/test_playback_hud.py -q
```

### 完成标准

- `tests/visual` 不再 segfault。
- 如果当前机器没有显示能力，可以 skip，但必须是 pytest skip，而不是进程崩溃。
- Visual gate 不能进入默认 full gate，除非它在 CI 环境有明确显示配置。

---

## Phase C：拆分 `src/app/main_window.py`

### 目标

把 9216 行 `MainWindow` 拆成 app shell + controllers/services，使功能禁用/删除真的可维护。

### 当前问题

`src/app/main_window.py` 仍 deep import 大量 feature internals：

```text
src.features.color.core.*
src.features.color.ui.*
src.features.edit.core.*
src.features.edit.ui.*
src.features.composite.core.*
src.features.composite.render.*
src.features.deliver.ui.*
```

这意味着虽然 `src` 目录变干净了，但 app 层边界还没有真正干净。

### 拆分原则

1. 不碰调色核心算法。
2. 不改变用户可见行为。
3. 不一次性大搬 9000 行。
4. 每次只抽一个 controller/service。
5. 每次抽完都跑 page context + feature flags + color session。

### 建议目标结构

```text
src/app/
  main_window.py
  window_lifecycle.py
  page_registry.py
  workspace_layout.py
  controllers/
    color_controller.py
    edit_controller.py
    composite_controller.py
    deliver_controller.py
    project_controller.py
    export_controller.py
    tool_panel_controller.py
  actions/
    app_actions.py
    color_actions.py
    edit_actions.py
    layer_actions.py
    playback_actions.py
  services/
    update_service.py
    visual_test_service.py
    shutdown_service.py
```

### 推荐拆分顺序

#### C1. 抽 update / startup / shutdown

先抽最容易影响 visual segfault 的生命周期逻辑。

目标文件：

```text
src/app/services/update_service.py
src/app/services/shutdown_service.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_page_context.py tests/visual/test_edit_page_visual.py -q
```

#### C2. 抽 page availability / feature flags

目标：

```text
src/app/page_registry.py
src/app/feature_registry.py
src/app/module_loader.py
```

统一 MainWindow 中页面可用性判断，不要散落 `if feature_flags`。

验证：

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py tests/test_page_context.py -q
```

#### C3. 抽 Edit controller

把 edit 相关 imports 和方法迁移到：

```text
src/app/controllers/edit_controller.py
```

包含：

- timeline 操作
- media pool 事件
- clip 参数更新
- edit page 初始化桥接

验证：

```bash
.venv/bin/python -m pytest tests/edit -q
.venv/bin/python -m pytest tests/test_page_context.py -q
```

#### C4. 抽 Composite controller

目标：

```text
src/app/controllers/composite_controller.py
```

包含：

- composite page 创建
- graph load/save bridge
- composite export renderer bridge

验证：

```bash
.venv/bin/python -m pytest tests/test_composite_graph.py tests/test_composite_preset.py tests/test_composite_project_migration.py -q
```

#### C5. 抽 Color panel/controller

这是最敏感阶段，必须保守。

目标：

```text
src/app/controllers/color_controller.py
src/features/color/facade.py
```

原则：

- 只搬 UI wiring。
- 不改 `ImageEngine`、`AsyncRenderer`、`ShaderPipeline` 行为。
- 不改 Layer 参数。

验证：

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

### 阶段完成标准

- `src/app/main_window.py` 降到 3000 行以下作为第一目标。
- app 层 deep import feature internals 数量减少 70% 以上。
- `tests/test_page_context.py`、feature flags、color session 持续通过。

---

## Phase D：统一 feature flag 系统

### 目标

当前项目里存在两套 feature flag 概念：

```text
src/app/feature_flags.py
src/shared/feature_flags.py
```

这容易让后续 AI 不知道该用哪一个。

### 当前观察

- `tests/test_feature_flags.py` 使用 `src.app.feature_flags` 和 `TOPOS_DISABLE_FEATURES`。
- `tests/test_feature_flags_registry.py` 使用 `src.shared.feature_flags` 和 `TOPOS_FEATURE_*`。
- `MainWindow` 当前 import `src.shared.feature_flags.FeatureFlags`。
- `src/main.py` 使用 feature module loading。

### 决策建议

保留一个 canonical API：

```text
src/shared/feature_flags.py
```

让 app 侧：

```text
src/app/feature_flags.py
```

变成薄 wrapper，或直接删除并迁移调用方。

### 兼容策略

同时支持两类环境变量一个版本周期：

```text
TOPOS_DISABLE_FEATURES=composite,deliver
TOPOS_FEATURE_COMPOSITE=false
TOPOS_FEATURE_DELIVER=false
```

优先级建议：

1. 显式 `TOPOS_FEATURE_<NAME>` 优先。
2. `TOPOS_DISABLE_FEATURES` 次之。
3. 默认全部开启。
4. `color` 默认不建议关闭，除非专门测试 isolation。

### 验证命令

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py -q
TOPOS_DISABLE_FEATURES=composite .venv/bin/python -m pytest tests/test_page_context.py -q
TOPOS_FEATURE_COMPOSITE=false .venv/bin/python -m pytest tests/test_page_context.py -q
```

完成标准：

- 只有一个权威 FeatureFlags 实现。
- app/module_loader/feature_registry/MainWindow 使用同一套结果。
- 文档中说明 feature flag 环境变量优先级。

---

## Phase E：让 app 层 deep import 变成可测试约束

### 目标

现在 `src/app/main_window.py` 仍大量导入 feature 内部模块。Phase C 会逐步减少；Phase E 要把这个目标变成测试约束。

### 当前不能立刻硬断言

如果现在直接禁止：

```text
from src.features.color.core
from src.features.edit.ui
from src.features.composite.core
```

会立即失败，因为 MainWindow 还没拆。

### 渐进策略

在 `tests/test_src_directory_boundaries.py` 中增加“预算型测试”：

```python
MAX_APP_DEEP_FEATURE_IMPORTS = 80
```

每完成一个 controller 拆分，就降低预算：

```text
80 -> 60 -> 40 -> 20 -> 0
```

不要一开始就要求 0。

### 验证命令

```bash
rg -n "from src\\.features\\.[^.]+\\.(core|ui|nodes|render|evaluators|adapters)|import src\\.features\\.[^.]+\\.(core|ui|nodes|render|evaluators|adapters)" src/app -g '*.py'
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

完成标准：

- app 层最终只允许 import：

```text
src.features.<feature>.module
src.features.<feature>.facade
src.features.<feature>.adapters.<public_adapter>
```

---

## Phase F：运行缓存和本地产物彻底移出 `src`

### 目标

虽然 Git 跟踪源码已经没有 `src/config.json`、`src/luts`，但本地仍会出现：

```text
src/.images_cache
src/__pycache__
```

`__pycache__` 正常，`.images_cache` 不理想。运行缓存不应该生成在源码目录。

### 操作步骤

1. 查 `.images_cache` 来源：

```bash
rg -n "\\.images_cache|images_cache|CACHE_DIR|cache_dir" src tests -g '*.py'
```

2. 建立统一 cache path：

```text
src/shared/paths/cache_paths.py
```

推荐位置：

```text
~/.cache/topos-color/
```

或项目根：

```text
.cache/topos-color/
```

3. 保留旧路径 fallback，但新写入不再写 `src/.images_cache`。

### 验证命令

```bash
rm -rf src/.images_cache
.venv/bin/python -m pytest tests/test_cache_fix.py tests/test_image_engine_state.py -q
test ! -d src/.images_cache
```

完成标准：

- 运行核心测试不会重新生成 `src/.images_cache`。
- `.gitignore` 保留防御性忽略，但不依赖忽略掩盖路径问题。

---

## Phase G：更新验收报告和计划文档

### 目标

把已经过时的数字修正，避免后续 AI 继续围绕不存在的问题工作。

### 需要修正的信息

1. Phase 10 当前本机结果：

```text
223 passed
```

不应继续写“25 failed，需要真实 GL 上下文”作为当前事实。

2. Phase 8 当前复核结果：

```text
97 passed, 4 skipped
```

不应写死 “120 passed”。

3. Visual 当前事实：

```text
tests/visual segfault
```

不应只写“需要显示器，已标记 visual”。

4. Test matrix marker 当前事实：

```text
-m color_core/composite_core/media_core 目前 0 selected
```

必须修复后再宣称 Phase 13 完成。

### 建议新增文档

```text
docs/validation_status_2026-05-27.md
```

内容：

- 已通过命令
- 失败命令
- 剩余风险
- 不再适用的旧结论

### 验证

文档提交即可，无测试要求。但建议同时跑：

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_feature_flags.py -q
```

---

## 4. 推荐下一轮提交顺序

### Commit 1：Fix acceptance marker gates

包含：

- `tests/conftest.py` marker 自动分配
- `tests/test_acceptance_markers.py`
- 更新 `docs/test_acceptance_matrix_2026-05-27.md`

验证：

```bash
.venv/bin/python -m pytest -m color_core --collect-only -q
.venv/bin/python -m pytest -m composite_core --collect-only -q
.venv/bin/python -m pytest -m media_core --collect-only -q
```

### Commit 2：Prevent visual tests from segfaulting

包含：

- MainWindow test mode 或 visual fixture 轻量启动
- shutdown/teardown 修复
- visual bootstrap smoke

验证：

```bash
.venv/bin/python -m pytest tests/visual -q
```

### Commit 3：Unify feature flags

包含：

- 合并 app/shared feature flag API
- 保留兼容 env var
- 更新 feature flag tests

验证：

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py tests/test_page_context.py -q
```

### Commit 4：Move runtime cache out of `src`

包含：

- cache path service
- `.images_cache` 新写入路径迁移
- cache tests

验证：

```bash
rm -rf src/.images_cache
.venv/bin/python -m pytest tests/test_cache_fix.py tests/test_image_engine_state.py -q
test ! -d src/.images_cache
```

### Commit 5+：MainWindow 拆分系列

每个 commit 只拆一个 controller/service：

```text
5a update/shutdown service
5b page registry / feature availability
5c edit controller
5d composite controller
5e deliver/export controller
5f color panel controller
```

每个 commit 验证：

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py tests/test_color_session.py -q
```

---

## 5. 给下一个 AI 的执行提示词

```text
你正在 Topos Color 项目中继续模块化后收尾优化。

请先阅读：
docs/post_ai_completion_remaining_plan_2026-05-27.md
docs/test_acceptance_matrix_2026-05-27.md

当前真实状态：
1. src/core、src/ui、src/composite 已删除。
2. src/config.json、src/luts 已不在 Git 跟踪源码中。
3. Composite 大范围逻辑测试 1325 passed。
4. YUV/GPU upload 相关测试当前本机 223 passed，不要再按“25 failed”处理。
5. tests/visual 当前仍会 segfault，必须先修到可失败但不崩溃。
6. pytest marker gate 当前没生效，-m color_core/composite_core/media_core 会 0 selected。
7. src/app/main_window.py 仍有 9216 行，是最大剩余重构任务。

请从 Phase A 开始：
- 修复 marker gate，让测试矩阵真正可执行。
- 然后 Phase B 修 visual segfault。
- 不要直接开始大拆 MainWindow。
- 不要改调色核心算法。
```

---

## 6. 最终完成定义

这一轮剩余优化完成时，应满足：

- `pytest -m color_core`、`pytest -m composite_core`、`pytest -m media_core` 不再 0 selected。
- `tests/visual` 不 segfault。
- `src/app/main_window.py` 降到 3000 行以下，或至少完成第一轮 controller 拆分并有预算测试。
- app 层 deep import feature internals 数量显著下降，并被测试预算约束。
- feature flag 只有一个 canonical API。
- 运行核心测试不会生成 `src/.images_cache`。
- 文档中的验收数字与当前命令结果一致。

