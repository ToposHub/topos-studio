# Topos Color 模块化后续优化计划表（2026-05-27）

> 目标：在已经完成 `src/app`、`src/features`、`src/shared` 主体瘦身之后，继续把项目从“目录看起来干净”推进到“边界真的稳定、测试可信、功能可插拔、合成页面可修复”的状态。
>
> 这份计划默认交给其它 AI 或开发者继续执行。每一步都必须小步提交、验证后再进入下一步。调色线管是当前软件核心能力，任何涉及 `src/features/color/core/` 的修改都必须优先保持行为不变，只做 import、adapter、测试、文档层面的安全整理。

---

## 0. 当前状态快照

### 0.1 已经完成的好变化

- `src/core` 顶层目录已经移除。
- `src/ui` 顶层目录已经移除。
- `src/composite` 顶层旧目录已经移除。
- 主结构已经接近目标：

```text
src/
  app/
  features/
    color/
    composite/
    deliver/
    edit/
    models/
    plugins/
    scripting/
  shared/
  main.py
```

- `tests/test_src_directory_boundaries.py` 已经通过。
- `src/shared` 对 `src.features` 的最后一个静态 import warning 已经修掉。
- `CompositePage` import smoke 已通过：

```bash
.venv/bin/python - <<'PY'
import importlib
module = importlib.import_module("src.features.composite.ui.composite_page")
getattr(module, "CompositePage")
print("OK composite page")
PY
```

### 0.2 当前未提交改动

当前工作区仍有一批验收修复未提交，主要是：

- `src/shared/project/layer_adapter.py`
- `src/shared/media/macos_vt/external_texture.py`
- 多个测试文件的旧路径修正
- benchmark 测试基座稳定性修正

正式进入后续优化前，必须先提交这批改动。

验证过的关键结果：

```bash
.venv/bin/python -m compileall -q src tests

.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_page_context.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  tests/test_composite_project_migration.py \
  tests/test_composite_video_frame_index.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  tests/media/test_decode_worker.py \
  tests/media/test_p1_1_disk_proxy_priority.py \
  tests/benchmark/test_latency.py \
  tests/benchmark/test_playback_performance.py \
  tests/benchmark/test_renderer_queue.py \
  tests/benchmark/test_ui_tick.py \
  -q
```

预期结果：

```text
475 passed
```

### 0.3 仍然存在的问题

这些问题不建议和“目录瘦身”混在一个提交中继续大改：

1. 全量测试存在大量历史失败，不全是本轮模块化引入。
2. `tests/visual/test_edit_page_visual.py` 在混跑中触发 PyQt/线程相关 segfault。
3. `tests --ignore=tests/media --ignore=tests/edit --ignore=tests/benchmark` 仍有大量失败，集中在：
   - composite 节点/ROI/tile/deep compositing
   - color tools/scripting API
   - ffmpeg encoder/export
   - playback/yuv/upload
   - sticky note/backdrop 等 UI 测试
4. `src/config.json`、`src/luts/` 仍在 `src` 顶层。它们不一定必须马上移走，但如果最终目标是 `src` 只保留代码入口，后续应迁移到 `src/shared/config` 或项目资源目录。
5. `src/__pycache__` 可能会被本地运行重新生成，不应纳入 Git。
6. `src/app/main_window.py` 仍然很可能承担过多职责，需要后续拆出 app services/controllers。
7. `features/models`、`features/plugins` 的归属需要确认：它们是功能域，还是 shared 能力，还是资源目录。

---

## 1. 总体优化原则

### 1.1 每轮只解决一类问题

推荐提交粒度：

1. 测试路径修复
2. 测试稳定性修复
3. app 壳瘦身
4. shared 边界清理
5. composite 功能修复
6. color 工具/脚本修复
7. export/ffmpeg 修复
8. visual/UI 测试稳定化

不要在一个提交里同时移动目录、改算法、改 UI、改测试。

### 1.2 调色核心线管必须冻结行为

以下文件在没有单独计划和完整回归前，禁止做行为级重构：

```text
src/features/color/core/image_engine.py
src/features/color/core/async_renderer.py
src/features/color/core/shader_compiler.py
src/features/color/core/layer.py
src/features/color/ui/canvas.py
src/shared/gpu/
src/shared/media/
src/shared/video/
```

允许做的安全操作：

- 改 import 路径
- 增加薄 adapter
- 增加 smoke test
- 增加边界测试
- 修复明显测试断言旧路径
- 修复不会改变输出的类型注解或空值保护

不允许混在本计划里的操作：

- 改调色算法
- 改 shader 生成逻辑
- 改 GPU 纹理生命周期
- 改渲染线程模型
- 改 layer 参数序列化语义

### 1.3 每阶段必须有退出标准

每阶段完成后至少执行：

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
git status --short
```

如果阶段涉及某个功能，还必须跑对应功能测试集。

---

## 2. 推荐执行顺序总览

```text
Phase 0  提交当前验收稳定点
Phase 1  建立“可信测试分层”
Phase 2  修复全量测试混跑崩溃和后台线程泄漏
Phase 3  清理历史测试旧路径和错误假设
Phase 4  继续瘦身 src 顶层剩余资源
Phase 5  app/main_window.py 职责拆分
Phase 6  shared 边界升级为强约束
Phase 7  composite 页面和节点系统专项修复
Phase 8  color tools / scripting API 专项修复
Phase 9  export / ffmpeg / deliver 专项修复
Phase 10 playback / yuv / gpu upload 专项修复
Phase 11 UI 视觉测试稳定化
Phase 12 插拔式功能开关与删除演练
Phase 13 最终 CI 验收矩阵
```

---

## Phase 0：提交当前验收稳定点

### 目标

把已经验证通过的模块化验收修复保存为一个干净提交，作为后续优化的回滚点。

### 操作步骤

1. 检查变更：

```bash
git status --short
git diff --stat
```

2. 跑关键验收：

```bash
.venv/bin/python -m compileall -q src tests

.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_page_context.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/test_alpha_system.py \
  tests/test_cache_fix.py \
  tests/test_composite_project_migration.py \
  tests/test_composite_video_frame_index.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  tests/media/test_decode_worker.py \
  tests/media/test_p1_1_disk_proxy_priority.py \
  tests/benchmark/test_latency.py \
  tests/benchmark/test_playback_performance.py \
  tests/benchmark/test_renderer_queue.py \
  tests/benchmark/test_ui_tick.py \
  -q
```

3. 提交：

```bash
git add src/shared/project/layer_adapter.py \
        src/shared/media/macos_vt/external_texture.py \
        tests/benchmark/benchmark_base.py \
        tests/benchmark/test_latency.py \
        tests/benchmark/test_renderer_queue.py \
        tests/benchmark/test_ui_tick.py \
        tests/media/test_color_frame_provider_adapter.py \
        tests/media/test_decode_worker.py \
        tests/media/test_p1_1_disk_proxy_priority.py \
        tests/test_alpha_system.py \
        tests/test_cache_fix.py \
        tests/test_composite_preset.py \
        tests/test_composite_project_migration.py \
        tests/test_composite_video_frame_index.py \
        tests/test_expression_parser.py \
        tests/test_keyframe.py \
        tests/test_merge_node.py \
        tests/test_reformat_crop_transform.py

git commit -m "Stabilize modularized import boundaries"
```

### 验证标准

- commit 成功。
- `git status --short` 只剩下本计划文件，或为空。
- 关键验收集保持 `475 passed`。

### 风险

- 如果后续 AI 直接跳过此阶段，后面所有修改会和当前验收修复混在一起，回滚困难。

---

## Phase 1：建立可信测试分层

### 目标

现在项目测试非常多，且全量混跑会触发历史失败和 PyQt segfault。下一步不是立刻修所有功能，而是先把测试分层，让每个优化任务有明确验收范围。

### 当前问题

全量命令：

```bash
.venv/bin/python -m pytest tests -q
```

会遇到：

- 大量历史失败。
- visual 测试 PyQt segfault。
- 后台线程未清理导致混跑不稳定。

### 建议新增文档

创建：

```text
docs/test_acceptance_matrix_2026-05-27.md
```

内容分层：

```text
Tier 0: import / compile / boundary
Tier 1: modularization smoke
Tier 2: core color session
Tier 3: composite graph and node pure logic
Tier 4: media/video pure unit
Tier 5: benchmark deterministic tests
Tier 6: UI/visual tests
Tier 7: full suite, currently not mandatory green
```

### 建议新增 pytest markers

检查 `pytest.ini`，补充或整理：

```ini
markers =
    modular: modularization boundary and smoke tests
    color_core: color pipeline safe regression tests
    composite_core: composite graph and pure node tests
    media_core: media/video pure unit tests
    benchmark: deterministic benchmark simulations
    visual: visual or PyQt screenshot tests
    unstable: known unstable tests
```

### 验收命令

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
.venv/bin/python -m pytest tests/test_color_session.py tests/test_composite_graph.py -q
.venv/bin/python -m pytest tests/benchmark/test_latency.py tests/benchmark/test_renderer_queue.py -q
```

### 完成标准

- 有一份清晰测试矩阵文档。
- 后续任何计划都能引用一个明确 Tier，而不是笼统说“跑全量测试”。

---

## Phase 2：修复全量混跑崩溃与后台线程泄漏

### 目标

让测试即使有失败，也不要 segfault 或卡住。测试失败可以修，进程崩溃会让验收失去意义。

### 已知症状

混跑到：

```text
tests/visual/test_edit_page_visual.py
```

出现 segmentation fault。堆栈里有：

```text
src/app/update_checker.py
src/shared/video/video_decoder.py
src/shared/video/video_async_renderer.py
tests/test_video_async_renderer.py
tests/visual/test_edit_page_visual.py
```

### 重点检查文件

```text
src/app/update_checker.py
src/shared/video/video_decoder.py
src/shared/video/video_async_renderer.py
tests/test_video_async_renderer.py
tests/visual/test_edit_page_visual.py
tests/conftest.py
```

### 修复方向

1. 测试环境禁止真实联网 update checker。
   - 在测试启动时设置环境变量，例如 `TOPOS_DISABLE_UPDATE_CHECK=1`。
   - `UpdateChecker.run()` 开头判断测试环境直接返回。
   - 不要让 visual fixture 创建窗口时自动访问网络。

2. 视频解码线程必须在测试 teardown 中关闭。
   - 检查 `VideoDecoder` 是否有 `close()` / `stop()`。
   - 检查 GLib main loop 是否退出。
   - 检查后台 `VideoAsyncRenderer` queue 线程是否 join。

3. 所有 PyQt visual fixture 必须保证：
   - 创建 QApplication 只创建一次。
   - fixture teardown 调用 `close()`。
   - 不在 teardown 后继续发 signal。
   - 不让 `QThread` 悬挂。

4. 对 known unstable visual 测试先加 marker：

```python
pytestmark = pytest.mark.visual
```

不要默认混入普通单元测试。

### 验证命令

先单跑：

```bash
.venv/bin/python -m pytest tests/visual/test_edit_page_visual.py -vv
.venv/bin/python -m pytest tests/test_video_async_renderer.py -vv
```

再混跑相关组：

```bash
.venv/bin/python -m pytest \
  tests/test_video_async_renderer.py \
  tests/visual/test_edit_page_visual.py \
  -vv
```

最后跑不包含 visual 的主测试：

```bash
.venv/bin/python -m pytest tests --ignore=tests/visual -q
```

### 完成标准

- 不再出现 segmentation fault。
- 即使测试失败，也必须输出 pytest failure summary。
- 后台线程在测试结束后不继续访问网络、GLib、renderer queue。

---

## Phase 3：清理历史测试旧路径和错误假设

### 目标

把测试中残留的旧路径引用改成新结构，避免误导后续 AI 以为 `src/core`、`src/ui`、顶层 `composite` 还存在。

### 当前扫描命令

```bash
rg -n "src/core|src/ui|from composite\\.|import composite\\.|from core\\.|import core\\." tests src -g '*.py'
```

### 已知残留类型

1. 只是 docstring 或注释。
2. hardcoded 绝对路径，指向旧项目：

```text
/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/core/...
```

3. 测试设计说明里的旧路径。
4. 真正 import 旧路径。

### 修复规则

- 注释/docstring 可以改，但单独提交。
- 真 import 必须改。
- hardcoded 旧绝对路径必须改成基于 `Path(__file__)` 的动态路径。
- 不要为了让测试过而恢复 `src/core` 或 `src/ui` wrapper。

### 示例修复

旧：

```python
source_path = "src/core/video/decode_worker.py"
```

新：

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
source_path = ROOT / "src/shared/video/decode_worker.py"
```

### 验证命令

```bash
rg -n "from composite\\.|import composite\\.|from core\\.|import core\\." tests src -g '*.py'
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

完成标准：

- 不再有真实旧 import。
- 旧路径只允许出现在迁移文档或历史说明中。

---

## Phase 4：继续瘦身 `src` 顶层剩余资源

### 目标

让 `src` 顶层更接近纯代码入口，减少运行产物和资源混放。

当前顶层：

```text
src/
  __init__.py
  app/
  config.json
  features/
  luts/
  main.py
  shared/
```

### 需要决策的对象

#### 4.1 `src/config.json`

建议目标：

```text
src/shared/config/default_config.json
```

或：

```text
resources/config/default_config.json
```

步骤：

1. 查引用：

```bash
rg -n "config\\.json|src/config" src tests -g '*.py'
```

2. 新增统一路径函数：

```text
src/shared/paths/app_paths.py
```

3. 所有读取都从路径函数走。
4. 保留旧路径 fallback 一个版本周期。

验证：

```bash
.venv/bin/python -m pytest tests/test_config_manager.py -q
.venv/bin/python -m compileall -q src tests
```

#### 4.2 `src/luts/`

它可能是调色核心资源，不能粗暴移动。

建议目标：

```text
resources/luts/
```

或：

```text
src/shared/assets/luts/
```

步骤：

1. 查引用：

```bash
rg -n "src/luts|luts/|LUT|lut" src tests -g '*.py'
```

2. 建立 LUT 查找服务：

```text
src/features/color/adapters/lut_paths.py
```

3. 搜索顺序：

```text
project user luts
resources/luts
legacy src/luts
```

4. 不改变 LUT 加载算法，只改变路径查找。

验证：

```bash
.venv/bin/python -m pytest tests/test_color_tools_lut_exporter.py -q
.venv/bin/python -m pytest tests/test_color_session.py -q
```

完成标准：

- `src` 顶层只剩：

```text
src/
  __init__.py
  app/
  features/
  main.py
  shared/
```

如果暂时保留 `config.json` 和 `luts/`，必须在 README 或 docs 中说明原因。

---

## Phase 5：`src/app/main_window.py` 职责拆分

### 目标

`app` 应是应用壳，不应把所有页面逻辑、菜单、工程读写、脚本、调色工具细节都堆在 `MainWindow`。

### 原则

- 不改 UI 行为。
- 不改调色核心。
- 只按职责搬函数。
- 每搬一组函数都跑 page/context 测试。

### 建议拆分目标

```text
src/app/
  main_window.py
  bootstrap.py
  page_registry.py
  menu/
    app_actions.py
    layer_actions.py
    playback_actions.py
  services/
    project_lifecycle.py
    page_context_service.py
    shortcut_service.py
    update_service.py
  ui/
    ...
```

### 拆分顺序

1. 先拆纯函数和 action 注册。
2. 再拆 page registry。
3. 再拆 project open/save 生命周期。
4. 最后拆跨页面协调逻辑。

### 每步验证

```bash
.venv/bin/python -m pytest tests/test_page_context.py -q
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
.venv/bin/python -m compileall -q src
```

### 完成标准

- `src/app/main_window.py` 行数明显下降。
- `src/app/main_window.py` 不再 deep import feature internals。
- MainWindow 更像 orchestration，而不是所有业务实现所在地。

---

## Phase 6：`shared` 边界升级为强约束

### 目标

目前 `shared` 不应依赖 feature。后续要把这个规则从“warning”变成“assert”。

### 当前状态

`layer_adapter.py` 已经用 `import_module()` 避开静态 import。

### 下一步

1. 查所有 shared 到 features 的依赖：

```bash
rg -n "src\\.features" src/shared -g '*.py'
```

2. 如果没有真实依赖，把 `tests/test_src_directory_boundaries.py` 里的 warning 改成 assert。

旧：

```python
if offenders:
    warnings.warn(...)
```

新：

```python
assert offenders == [], ...
```

3. 所有确实需要访问 feature 的地方必须通过：

```text
features/<name>/module.py
features/<name>/adapters/
app/page_registry.py
```

### 验证

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

完成标准：

- shared -> features 静态 import 被测试强制禁止。

---

## Phase 7：Composite 页面与节点系统专项修复

### 目标

在 `src` 瘦身稳定后，再集中修 composite。不要把 composite 修复和目录迁移混在一起。

### 当前优先级

1. 页面能打开。
2. 节点图能创建默认 graph。
3. 基础节点能拖入、连接、删除、undo。
4. 输出 viewport 不黑屏。
5. preset/migration/load/save 不丢节点。
6. tile/ROI/cache/deep compositing 逐项修。

### 已知失败集中区

从非 media/edit/benchmark 测试混跑看，失败集中在：

```text
tests/test_composite_nodes_merge.py
tests/test_composite_nodes_text.py
tests/test_composite_roi.py
tests/test_composite_tile.py
tests/test_deep_compositing.py
tests/test_node_capabilities.py
tests/test_node_graph_commands.py
```

### 修复顺序

#### 7.1 Composite import 和页面 smoke

```bash
.venv/bin/python - <<'PY'
import importlib
for module_name in [
    "src.features.composite.module",
    "src.features.composite.ui.composite_page",
    "src.features.composite.core.node_engine",
]:
    importlib.import_module(module_name)
    print("OK", module_name)
PY
```

#### 7.2 Node registry 稳定

重点文件：

```text
src/features/composite/core/node_engine.py
src/features/composite/nodes/
src/features/composite/core/node_capabilities.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_composite_graph.py tests/test_composite_nodes_registry.py -q
```

#### 7.3 Merge/Text/ROI/Tile 分批修

每批只修一个测试文件：

```bash
.venv/bin/python -m pytest tests/test_composite_nodes_merge.py -q
.venv/bin/python -m pytest tests/test_composite_nodes_text.py -q
.venv/bin/python -m pytest tests/test_composite_roi.py -q
.venv/bin/python -m pytest tests/test_composite_tile.py -q
```

#### 7.4 Deep compositing

只在基础节点和 ROI/tile 通过后处理：

```bash
.venv/bin/python -m pytest tests/test_deep_compositing.py -q
```

### 完成标准

```bash
.venv/bin/python -m pytest \
  tests/test_composite_graph.py \
  tests/test_composite_nodes_*.py \
  tests/test_composite_roi.py \
  tests/test_composite_tile.py \
  tests/test_deep_compositing.py \
  -q
```

尽量全绿；如果某些测试本身过时，必须更新测试说明，不要静默跳过。

---

## Phase 8：Color tools / scripting API 专项修复

### 目标

修复调色工具和 scripting API 测试，但不动调色核心算法。

### 已知失败集中区

```text
tests/test_color_tools_lut_exporter.py
tests/test_color_tools_params.py
tests/test_color_tools_scripting_api.py
tests/test_image_engine_params.py
```

### 安全修复边界

允许：

- 参数 schema 对齐
- 测试中的旧路径修复
- scripting adapter 的薄封装
- UI tool 参数同步 bug 修复
- 空值和默认值保护

谨慎：

- `ImageEngine` 参数默认值变更
- Layer serialization 语义变更
- shader param 名称变更

禁止：

- 未经像素回归就改 shader 公式
- 未经 session 回归就改 layer 合成顺序

### 推荐命令

```bash
.venv/bin/python -m pytest tests/test_color_session.py -q
.venv/bin/python -m pytest tests/test_color_tools_params.py -q
.venv/bin/python -m pytest tests/test_color_tools_scripting_api.py -q
.venv/bin/python -m pytest tests/test_image_engine_params.py -q
```

### 验证标准

- `tests/test_color_session.py` 必须始终通过。
- 修 color tools 后再跑：

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_image_engine_state.py -q
```

---

## Phase 9：Export / FFmpeg / Deliver 专项修复

### 目标

把导出链路从目录迁移后的不稳定状态修回到可验收。

### 已知失败集中区

```text
tests/test_export_frame_provider.py
tests/test_ffmpeg_encoder.py
tests/test_save_router.py
```

### 重点文件

```text
src/features/deliver/
src/shared/export/
src/shared/project/save_router.py
src/shared/media/
```

### 修复顺序

1. 先修纯导出 provider。
2. 再修 ffmpeg encoder 参数构造。
3. 最后修 save router 和工程包集成。

### 验证

```bash
.venv/bin/python -m pytest tests/test_export_frame_provider.py -q
.venv/bin/python -m pytest tests/test_ffmpeg_encoder.py -q
.venv/bin/python -m pytest tests/test_save_router.py -q
```

### 风险

FFmpeg 测试可能依赖本机 ffmpeg/codec 能力。需要区分：

- 代码错误
- 环境缺依赖
- 测试假设过旧

不要把环境问题硬改成代码行为。

---

## Phase 10：Playback / YUV / GPU upload 专项修复

### 目标

修复 video texture pool、YUV upload、临时纹理释放等底层媒体测试，但不要影响调色核心渲染。

### 已知失败集中区

```text
tests/test_video_texture_pool.py
tests/test_video_upload_command.py
tests/test_yuv_chroma_sampling_pixels.py
tests/test_yuv_gpu_upload.py
tests/test_yuv_temp_texture_release.py
tests/test_yuv_upload_api.py
```

### 重点文件

```text
src/shared/video/
src/shared/media/
src/shared/gpu/
src/features/color/core/async_renderer.py
```

### 修复原则

- 纹理生命周期优先于测试绿。
- 不能用“不释放资源”换测试通过。
- 不能在 UI 线程里引入 OpenGL 操作。
- 不能让 CPU fallback 假装 zero-copy。

### 验证

```bash
.venv/bin/python -m pytest tests/test_video_texture_pool.py -q
.venv/bin/python -m pytest tests/test_yuv_upload_api.py -q
.venv/bin/python -m pytest tests/test_yuv_gpu_upload.py -q
.venv/bin/python -m pytest tests/media/test_gpu_frame.py -q
```

完成后再跑：

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_video_async_renderer.py -q
```

---

## Phase 11：UI 视觉测试稳定化

### 目标

把 PyQt visual 测试从“会导致整进程崩溃”改成“可单独运行、失败可诊断”。

### 重点文件

```text
tests/visual/
tests/test_backdrop.py
tests/test_sticky_note.py
tests/test_playback_hud.py
```

### 修复步骤

1. 所有 visual 测试加 marker：

```python
pytestmark = pytest.mark.visual
```

2. 所有 fixture teardown 明确关闭窗口：

```python
widget.close()
widget.deleteLater()
qtbot.wait(0)
```

3. 禁止 visual fixture 自动启动：
   - update checker
   - video decoder
   - async renderer
   - 网络请求

4. 如果必须创建 MainWindow，使用 test mode：

```python
MainWindow(test_mode=True)
```

如果当前没有 `test_mode`，新增 app-level test config，不要在生产逻辑里散落 `if pytest`。

### 验证

```bash
.venv/bin/python -m pytest tests/visual -q
.venv/bin/python -m pytest tests/test_backdrop.py tests/test_sticky_note.py tests/test_playback_hud.py -q
```

完成标准：

- 单独跑 visual 不 segfault。
- 和 `tests/test_video_async_renderer.py` 混跑不 segfault。

---

## Phase 12：功能可插拔与删除演练

### 目标

验证最初目标：未来可以禁用某个功能，甚至删除某项功能文件夹，不影响其它功能基本启动。

### 12.1 Feature manifest

每个 feature 增加统一声明：

```text
src/features/color/module.py
src/features/edit/module.py
src/features/composite/module.py
src/features/deliver/module.py
src/features/scripting/module.py
```

需要提供：

```python
FEATURE_ID = "color"
FEATURE_NAME = "Color"
REQUIRES = []

def register(app_context): ...
def create_page(parent=None): ...
def get_actions(): ...
```

注意：不必一次完成所有方法，先统一最小接口。

### 12.2 Feature flags

建立：

```text
src/app/feature_flags.py
```

示例：

```python
ENABLED_FEATURES = {
    "color": True,
    "edit": True,
    "composite": True,
    "deliver": True,
    "scripting": True,
}
```

### 12.3 禁用演练

用环境变量测试：

```bash
TOPOS_DISABLE_FEATURES=composite .venv/bin/python -m pytest tests/test_page_context.py -q
TOPOS_DISABLE_FEATURES=deliver .venv/bin/python -m pytest tests/test_page_context.py -q
```

### 12.4 删除演练

不要真的删除用户目录。使用临时复制目录或 import blocking 模拟：

```python
with block_import_prefix("src.features.composite"):
    ...
```

完成标准：

- 禁用 composite 后，color/edit/deliver 基础 import 不失败。
- 禁用 deliver 后，color/edit/composite 基础 import 不失败。
- color 不应依赖 composite。
- shared 不应依赖任何 feature。

---

## Phase 13：最终 CI 验收矩阵

### 目标

建立一个可以长期执行的验收矩阵，不再依靠人工临时挑测试。

### 推荐命令

#### Fast gate

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
.venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py -q
```

#### Modular gate

```bash
.venv/bin/python -m pytest \
  tests/test_src_directory_boundaries.py \
  tests/test_modular_import_boundaries.py \
  tests/test_page_context.py \
  -q
```

#### Color gate

```bash
.venv/bin/python -m pytest \
  tests/test_color_session.py \
  tests/test_image_engine_state.py \
  tests/test_image_engine_layers.py \
  tests/test_image_engine_params.py \
  -q
```

#### Composite gate

```bash
.venv/bin/python -m pytest \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  tests/test_composite_project_migration.py \
  tests/test_expression_parser.py \
  tests/test_keyframe.py \
  tests/test_merge_node.py \
  tests/test_reformat_crop_transform.py \
  -q
```

#### Media gate

```bash
.venv/bin/python -m pytest \
  tests/media/test_decode_worker.py \
  tests/media/test_p1_1_disk_proxy_priority.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/media/test_video_frame_provider_service.py \
  tests/media/test_video_frame_queue.py \
  -q
```

#### Benchmark gate

```bash
.venv/bin/python -m pytest \
  tests/benchmark/test_latency.py \
  tests/benchmark/test_playback_performance.py \
  tests/benchmark/test_renderer_queue.py \
  tests/benchmark/test_ui_tick.py \
  -q
```

#### Visual gate

```bash
.venv/bin/python -m pytest tests/visual -q
```

只有 visual gate 稳定后，才把它并入 full gate。

---

## 建议提交顺序

```text
Commit 1: Stabilize current modularization fixes
Commit 2: Document test acceptance tiers
Commit 3: Prevent update checker and video threads in tests
Commit 4: Migrate remaining legacy test paths
Commit 5: Move config/lut lookup behind path services
Commit 6: Split app action/page services from MainWindow
Commit 7: Enforce shared boundary assertions
Commit 8: Repair composite core tests
Commit 9: Repair color tools tests without color core behavior changes
Commit 10: Repair export/ffmpeg tests
Commit 11: Repair playback/yuv upload tests
Commit 12: Stabilize visual tests
Commit 13: Add feature flag disable/delete smoke tests
```

---

## 给下一个 AI 的执行提示词

```text
你正在 Topos Color 项目中继续模块化后的优化。

项目技术栈：
- Python
- PyQt6
- ModernGL
- GLSL
- 图像/视频调色、剪辑、合成软件

关键约束：
1. 调色线管是核心功能，禁止随意改 src/features/color/core/image_engine.py、async_renderer.py、shader_compiler.py、layer.py 的行为。
2. 每次只做一个 Phase，不要跨阶段混改。
3. 不要恢复 src/core、src/ui、src/composite 顶层旧目录。
4. shared 不能静态 import src.features。
5. app 只能通过 feature module/adapter/runtime 使用功能，不要 deep import feature internals。
6. 每一步完成后必须运行对应验证命令，并记录结果。

当前建议从 Phase 0 开始：
- 先提交当前已经通过验收的修复。
- 然后执行 Phase 1：建立测试验收矩阵。
```

---

## 最终完成定义

当以下条件全部满足时，才算这一轮后续优化完成：

- `src` 顶层没有运行产物目录。
- `src/core`、`src/ui`、`src/composite` 不存在。
- `src/shared` 静态 import `src.features` 被测试强制禁止。
- `src/app` 不 deep import feature internals。
- `CompositePage` 可以 import，并能进入页面 smoke。
- `ColorSession` 和调色核心测试保持通过。
- visual 测试不会 segfault。
- 有明确 CI/test gate 文档。
- 至少能禁用 `composite` 或 `deliver` 后启动其它基础功能 smoke。

