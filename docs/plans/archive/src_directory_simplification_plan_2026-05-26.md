# Topos Color `src/` 目录极简化实施计划（2026-05-26）

> 目标：把当前仍然偏“历史目录 + 兼容 wrapper + feature 目录混合”的 `src/`，收敛到清晰、可维护、可禁用功能的结构：

```text
src/
  app/                 # 应用壳：启动、菜单、页面注册、布局
  shared/              # 公共基础能力：媒体、GPU抽象、keyframe、项目schema、i18n、配置
  features/
    edit/
      core/
      ui/
      adapters/
    color/
      core/
      ui/
      shaders/
      adapters/
    composite/
      core/
      ui/
      nodes/
      render/
      evaluators/
      utils/
      adapters/
    deliver/
      core/
      ui/
      adapters/
```

本计划的重点不是“让功能先跑起来”，而是让 `src/` 顶层变得真正简洁：旧目录要么迁入 `features/`，要么迁入 `shared/`，要么只保留短期 wrapper，最终从顶层消失。

---

## 0. 当前状态快照

### 当前 `src/` 顶层仍存在的问题

当前 `src/` 仍包含这些顶层目录或文件：

```text
src/
  app/
  assets/
  composite/
  core/
  features/
  locales/
  scripting/
  shared/
  ui/
  utils/
  __init__.py
  config.json
  main.py
  metadata.db
  topos.db
```

距离目标结构的问题：

| 当前路径 | 问题 | 最终去向 |
|---|---|---|
| `src/core/` | 仍是最大历史混合目录，既有 Color 核心，也有 video/media/edit/export/project | 拆到 `features/color/core`、`features/edit/core`、`shared/*` |
| `src/ui/` | 仍是历史 UI 大杂烩，Color 面板、MainWindow、widgets、旧 Edit wrapper 混在一起 | `app/`、`features/color/ui`、`shared/ui`、`features/edit/ui` |
| `src/utils/` | 配置、i18n、日志、图标、路径等公共工具散在顶层 | `shared/config`、`shared/i18n`、`shared/logging`、`shared/assets` |
| `src/composite/` | 已基本是兼容 wrapper，但仍占据顶层 | 短期保留，最终删除或移到 `compat/` |
| `src/assets/` | 顶层资源目录，和 feature/shared 边界不清 | `shared/assets` 或 `app/assets` |
| `src/locales/` | 顶层国际化资源 | `shared/i18n/locales` |
| `src/scripting/` | 脚本系统在顶层 | `features/scripting` 或 `shared/scripting`，需先判断是否独立功能 |
| `src/main.py` | 启动入口放在顶层 | `app/main.py`，顶层保留薄 wrapper 或包入口 |
| `src/config.json` | 运行配置文件不应在源码目录 | 项目根 `config/`、用户目录，或 `app/default_config.json` |
| `src/*.db` | 数据库文件不应在源码目录 | 项目根 `data/` 或用户数据目录，不能继续放源码树 |

### 当前 `features/` 已具备基础

已有：

```text
src/features/color/
  adapters/
  ui/

src/features/edit/
  core/
  ui/
  adapters/

src/features/composite/
  core/
  ui/
  nodes/
  render/
  evaluators/
  utils/
  adapters/

src/features/deliver/
  core/
  ui/
  adapters/
```

说明：

- Composite 方向已经比较完整。
- Edit 已经有 `core/ui/adapters`。
- Deliver 已经有 `core/ui/adapters`。
- Color 目前只有 `ui/adapters`，真正核心仍在 `src/core` 和 `src/ui`，这是下一阶段最大工作量。

---

## 1. 核心原则

### 1.1 不追求一次性删除旧目录

这次目标是让 `src/` 最终简洁，但实施时必须分阶段：

1. 先迁真实实现到新位置。
2. 再把旧路径改成 wrapper。
3. 再更新测试和应用引用。
4. 最后做删除演练。
5. 删除旧目录。

任何跳过 wrapper 阶段的“大搬家”，都很容易导致旧测试、项目保存格式、插件路径、用户配置直接断掉。

### 1.2 Color 管线保护优先级最高

这些文件涉及当前软件最核心的调色链路：

```text
src/core/image_engine.py
src/core/async_renderer.py
src/core/shader_compiler.py
src/core/gl_backend.py
src/core/color_output.py
src/core/layer.py
src/core/shaders.py
src/core/shader_nodes.py
src/ui/canvas.py
src/ui/gl_viewport.py
```

迁移这些文件时，只做路径移动和 import 适配，不做行为重写。

### 1.3 顶层只允许保留少数目录

最终 `src/` 顶层建议只保留：

```text
src/
  app/
  shared/
  features/
  __init__.py
```

可选短期保留：

```text
src/compat/
```

如果项目需要继续支持旧 import，例如 `src.composite.*`、`src.core.*`、`src.ui.*`，建议把 wrapper 统一收敛到 `src/compat/legacy/`，然后由一层 import hook 或明确 wrapper 管理。不要让 `src/core`、`src/ui`、`src/composite` 永久占据顶层。

---

## 2. 最终目录设计

### 2.1 推荐最终结构

```text
src/
  app/
    __init__.py
    main.py
    bootstrap.py
    main_window.py
    layout/
    menus/
    module_loader.py
    feature_registry.py
    page_contract.py
    app_context.py

  shared/
    __init__.py
    assets/
      icons/
      images/
      styles/
    config/
      config_manager.py
      default_config.json
    db/
      database.py
    effects/
      preview_quality.py
    export/
    gpu/
      gl_backend.py
      gpu_types.py
    i18n/
      i18n.py
      locales/
    logging/
      log_manager.py
    media/
    paths/
      paths.py
    project/
    scripting/
    ui/
      widgets/
      dialogs/
    utils/
      geometry_math.py
      thumbnail_provider.py
      update_checker.py
    video/

  features/
    color/
      __init__.py
      module.py
      core/
        image_engine.py
        async_renderer.py
        color_output.py
        color_manager.py
        color_session.py
        cache_manager.py
        denoise_engine.py
        history.py
        layer.py
        lut_exporter.py
        plugin_manager.py
        shader_compiler.py
        shader_nodes.py
      ui/
        adjustment_panel.py
        blur_sharpen_panel.py
        canvas.py
        crop_panel.py
        deblur_panel.py
        denoise_panel.py
        film_grain_panel.py
        grading_panel.py
        hsl_curves_panel.py
        layer_panel.py
        mixer_panel.py
        plugin_panel.py
        presets_panel.py
        raw_panel.py
        scope_panel.py
        tools_panel.py
        warper_panel.py
      shaders/
        shaders.py
      adapters/

    edit/
      __init__.py
      module.py
      core/
      ui/
      adapters/

    composite/
      __init__.py
      module.py
      runtime.py
      core/
      ui/
      nodes/
      render/
      evaluators/
      utils/
      adapters/

    deliver/
      __init__.py
      module.py
      core/
      ui/
      adapters/
```

### 2.2 不建议的结构

不要继续保留：

```text
src/core/
src/ui/
src/utils/
src/composite/
```

除非它们在某个过渡提交中只作为 wrapper 存在，并且有删除日期。

---

## 3. 阶段 A：源码树清洁和禁止生成文件回流

### 目标

先保证 `src/` 里没有本地运行产物、缓存、数据库文件继续污染视图。

### 当前要处理

```text
src/.DS_Store
src/__pycache__/
src/**/__pycache__/
src/**/*.pyc
src/config.json
src/metadata.db
src/topos.db
```

### 实施步骤

1. 确认这些文件是否被 git 跟踪：

```bash
git ls-files | rg '(^src/(config\\.json|metadata\\.db|topos\\.db)$|__pycache__|\\.pyc$|\\.DS_Store$)' || true
```

2. 删除未跟踪运行产物：

```bash
find src -name '__pycache__' -type d -prune -exec rm -rf {} +
find src -name '*.pyc' -type f -delete
find src -name '.DS_Store' -type f -delete
```

3. 如果 `src/config.json`、`src/metadata.db`、`src/topos.db` 被跟踪，不要直接删除，先迁移：

```text
src/config.json       -> config/default_config.json 或 src/shared/config/default_config.json
src/metadata.db       -> data/metadata.db（如果是开发样例）或从仓库移除
src/topos.db          -> data/topos.db（如果是开发样例）或从仓库移除
```

4. 更新 `.gitignore`：

```text
__pycache__/
*.py[cod]
.DS_Store
*.db
src/*.db
```

注意：如果确实需要样例数据库，改名为：

```text
tests/fixtures/*.db
```

### 验证

```bash
git status --short --untracked-files=all
git ls-files | rg '(__pycache__|\\.pyc$|\\.DS_Store$)' && exit 1 || true
```

### 完成标准

- `src/` 中没有本地缓存文件。
- 数据库文件不再位于源码目录。
- 后续 `compileall` 生成的缓存不会进入 git status。

---

## 4. 阶段 B：`src/app` 成为唯一应用壳

### 目标

把启动、MainWindow、菜单、布局、页面注册、FeatureRegistry 全部收敛到 `src/app`。

### 当前相关路径

```text
src/main.py
src/ui/main_window.py
src/app/module_loader.py
src/app/feature_registry.py
src/app/page_contract.py
src/app/app_context.py
```

### 目标路径

```text
src/app/main.py
src/app/main_window.py
src/app/bootstrap.py
src/app/module_loader.py
src/app/feature_registry.py
src/app/page_contract.py
src/app/app_context.py
src/main.py              # 短期 wrapper，可最终删除
src/ui/main_window.py    # 短期 wrapper，可最终删除
```

### 实施步骤

1. 新建 `src/app/main_window.py`，从 `src/ui/main_window.py` 迁入真实实现。

2. 将旧文件改为 wrapper：

```python
"""Compatibility wrapper for the app main window."""
from src.app.main_window import *
```

3. 新建 `src/app/main.py`，从 `src/main.py` 迁入真实启动逻辑。

4. 将 `src/main.py` 改为 wrapper：

```python
"""Compatibility wrapper for application entrypoint."""
from src.app.main import *
```

5. 全局替换应用内部 import：

```text
from src.ui.main_window import MainWindow
-> from src.app.main_window import MainWindow

from src.main import ...
-> from src.app.main import ...
```

6. 暂时保留测试中的旧 import，直到 wrapper 稳定后再迁测试。

### 验证

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_feature_flags_registry.py tests/test_config_manager.py tests/test_page_context.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from src.app.main_window import MainWindow
print(MainWindow)
PY
```

### 完成标准

- 真实 MainWindow 在 `src/app/main_window.py`。
- `src/ui/main_window.py` 只剩 wrapper。
- 应用启动入口真实实现位于 `src/app/main.py`。
- 顶层 `src/main.py` 可被后续删除。

---

## 5. 阶段 C：Color 功能迁移到 `features/color`

### 目标

这是本计划最重要、风险最高的一步。目标是让调色核心真正属于：

```text
src/features/color/
  core/
  ui/
  shaders/
  adapters/
```

### 需要迁移的核心文件

从 `src/core/` 迁到 `src/features/color/core/`：

```text
src/core/image_engine.py
src/core/async_renderer.py
src/core/color_output.py
src/core/color_manager.py
src/core/color_session.py
src/core/cache_manager.py
src/core/denoise_engine.py
src/core/history.py
src/core/layer.py
src/core/lut_exporter.py
src/core/plugin_manager.py
src/core/shader_compiler.py
src/core/shader_nodes.py
```

从 `src/core/` 迁到 `src/features/color/shaders/`：

```text
src/core/shaders.py
src/core/shaders/
```

从 `src/ui/` 迁到 `src/features/color/ui/`：

```text
src/ui/adjustment_panel.py
src/ui/blur_sharpen_panel.py
src/ui/canvas.py
src/ui/crop_panel.py
src/ui/deblur_panel.py
src/ui/denoise_panel.py
src/ui/film_grain_panel.py
src/ui/gl_viewport.py
src/ui/grading_panel.py
src/ui/hsl_curves_panel.py
src/ui/layer_panel.py
src/ui/mixer_panel.py
src/ui/plugin_panel.py
src/ui/presets_panel.py
src/ui/scope_panel.py
src/ui/tools_panel.py
src/ui/warper_panel.py
```

说明：实际文件名以当前仓库为准，执行前用 `find src/ui -maxdepth 1 -type f -name "*.py"` 确认。

### 不要迁到 Color 的文件

这些不是 Color 专属：

```text
src/core/media/      -> src/shared/media/
src/core/video/      -> src/shared/video/
src/core/project/    -> src/shared/project/
src/core/edit/       -> src/features/edit/core/ 或 wrapper-only
src/core/export/     -> src/shared/export/ 或 wrapper-only
src/core/file_io.py  -> src/shared/io/file_io.py
src/core/save_router.py -> src/app/save_router.py 或 src/shared/project/save_router.py
src/core/gl_backend.py  -> src/shared/gpu/gl_backend.py
```

### 实施步骤

#### C1. 创建目标目录

```bash
mkdir -p src/features/color/core src/features/color/ui src/features/color/shaders src/features/color/adapters
```

#### C2. 先搬 UI 面板

优先迁移低风险 UI 面板，不先碰 `image_engine.py`。

示例：

```bash
git mv src/ui/adjustment_panel.py src/features/color/ui/adjustment_panel.py
```

旧路径加 wrapper：

```python
"""Compatibility wrapper for Color UI adjustment panel."""
from src.features.color.ui.adjustment_panel import *
```

#### C3. 搬 Color core

对每个核心文件执行：

1. `git mv`
2. 更新内部 import
3. 旧路径 wrapper
4. 单文件 import smoke test
5. 跑 Color 核心测试

示例：

```bash
git mv src/core/color_output.py src/features/color/core/color_output.py
```

旧路径：

```python
"""Compatibility wrapper for ColorOutput."""
from src.features.color.core.color_output import *
```

#### C4. 搬 shader 文件

迁移：

```text
src/core/shaders.py -> src/features/color/shaders/shaders.py
src/core/shaders/   -> src/features/color/shaders/
```

旧路径 wrapper：

```python
from src.features.color.shaders.shaders import *
```

#### C5. 更新 MainWindow import

MainWindow 应从：

```text
src.features.color.ui.*
src.features.color.core.*
```

导入 Color 专属内容，不再从 `src.ui.*` 或 `src.core.*` 导入 Color 专属内容。

### 每小步验证

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_config_manager.py -q
```

涉及 shader/compiler 时额外跑：

```bash
.venv/bin/python -m pytest tests/test_color_tools_params.py tests/test_async_renderer_lane_merge.py -q
```

如果有 GUI smoke 能跑：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from src.features.color.core.image_engine import ImageEngine
print(ImageEngine)
PY
```

### 完成标准

- Color 真实实现位于 `features/color`。
- `src/core/image_engine.py` 等旧文件只剩 wrapper。
- `src/ui/canvas.py` 等 Color UI 旧文件只剩 wrapper。
- 调色核心测试全部通过。
- 不发生任何调色参数或 shader 行为改写。

---

## 6. 阶段 D：共享基础能力迁入 `src/shared`

### 目标

把跨功能基础能力从 `src/core`、`src/ui`、`src/utils` 中抽出来，统一进入 `shared`。

### 6.1 Media

当前：

```text
src/core/media/
```

目标：

```text
src/shared/media/
```

步骤：

```bash
git mv src/core/media src/shared/media
```

旧路径：

```text
src/core/media/*.py -> wrapper 到 src.shared.media.*
```

验证：

```bash
.venv/bin/python -m pytest tests/media -q
```

### 6.2 Video

当前：

```text
src/core/video/
src/core/video_decoder.py
src/core/video_playback.py
src/core/video_texture_pool.py
src/core/video_upload.py
src/core/video_async_renderer.py
src/core/yuv_gpu_upload.py
```

目标：

```text
src/shared/video/
```

注意：

- 如果某些 video 文件强依赖 Color renderer，需要先放在 `features/color/core/video_bridge.py` 或 `shared/video/adapters/color_adapter.py`。
- 不要把视频播放逻辑硬塞进 Color，除非它只服务调色预览。

验证：

```bash
.venv/bin/python -m pytest tests/test_video_upload_command.py tests/test_video_pipeline_decision.py tests/test_video_pipeline_fallback_types.py tests/media -q
```

### 6.3 GPU

当前：

```text
src/core/gl_backend.py
```

目标：

```text
src/shared/gpu/gl_backend.py
```

风险：

- Color、Composite、可能还有 Viewer 都会用它。
- 先迁移，旧路径 wrapper 保留较长时间。

验证：

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_image_engine_state.py tests/test_render_scheduler.py tests/test_nodes -q
```

### 6.4 Config / i18n / logging / paths

当前：

```text
src/utils/config_manager.py
src/utils/i18n.py
src/utils/log_manager.py
src/utils/paths.py
src/utils/database.py
src/utils/icon_manager.py
src/utils/shortcut_manager.py
src/utils/style_sheet.py
```

目标：

```text
src/shared/config/config_manager.py
src/shared/i18n/i18n.py
src/shared/logging/log_manager.py
src/shared/paths/paths.py
src/shared/db/database.py
src/shared/assets/icon_manager.py
src/shared/ui/shortcut_manager.py
src/shared/ui/style_sheet.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_modular_import_boundaries.py -q
```

### 完成标准

- `src/utils` 只剩 wrapper，最后可删除。
- 公共能力都能从 `src.shared.*` 导入。
- Feature 内部不再 import `src.utils.*`，改用 `src.shared.*`。

---

## 7. 阶段 E：Edit / Deliver 旧 wrapper 收口

### Edit

当前可能仍存在：

```text
src/core/edit/
src/ui/edit/
```

目标：

```text
src/features/edit/core/
src/features/edit/ui/
src/features/edit/adapters/
```

步骤：

1. 确认真实实现是否已经在 `features/edit`：

```bash
find src/features/edit -maxdepth 3 -type f -name "*.py" | sort
```

2. 如果 `src/core/edit` 和 `src/ui/edit` 只是 wrapper，迁移测试引用到新路径。

3. 添加边界测试：

```text
src/features/edit 不允许 import src.core.edit 或 src.ui.edit
```

4. 做删除演练：

```bash
mv src/core/edit /tmp/topos_old_core_edit
mv src/ui/edit /tmp/topos_old_ui_edit
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/edit -q
mv /tmp/topos_old_core_edit src/core/edit
mv /tmp/topos_old_ui_edit src/ui/edit
```

### Deliver

当前可能仍存在：

```text
src/core/export/
src/ui/export_panel.py
src/ui/export_dialog.py
src/ui/quick_export_dialog.py
src/ui/deliver_page.py
```

目标：

```text
src/features/deliver/core/
src/features/deliver/ui/
src/features/deliver/adapters/
src/shared/export/     # 纯导出基础设施
```

规则：

- UI 页面属于 `features/deliver/ui`。
- 编码器、presets、sequence exporter 等跨页面可用能力属于 `shared/export`。

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_timeline_export.py tests/test_feature_flags_registry.py -q
```

### 完成标准

- `src/core/edit`、`src/ui/edit`、`src/core/export`、旧 export UI 文件只剩 wrapper 或已删除。
- Edit / Deliver 禁用时，其它功能不受影响。

---

## 8. 阶段 F：Composite 顶层 wrapper 最终处理

### 当前状态

`src/composite` 已基本 wrapper-only，但它仍让 `src/` 顶层看起来不干净。

### 两个可选方案

#### 方案 1：保留到最后再删（推荐）

优点：

- 老测试、老插件、旧项目脚本不容易断。
- 迁移风险低。

做法：

1. 把测试 import 全部从 `src.composite.*` 迁到 `src.features.composite.*`。
2. 保留一轮提交。
3. 做删除演练。
4. 删除 `src/composite`。

#### 方案 2：移入 `src/compat/composite`

优点：

- `src/` 顶层立刻更干净。

缺点：

- Python import 兼容需要额外处理，不一定值得。

不推荐在没有 import hook 的情况下直接移动，因为 `src.composite` 包名会消失。

### 推荐步骤

1. 迁移测试 import：

```bash
rg -n "src\\.composite|from composite|import composite" tests -g "*.py"
```

2. 分批替换：

```text
src.composite.nodes.* -> src.features.composite.nodes.*
src.composite.ui.* -> src.features.composite.ui.*
src.composite.render_scheduler -> src.features.composite.render.render_scheduler
src.composite.graph_snapshot -> src.features.composite.core.graph_snapshot
```

3. 跑完整 Composite 测试。

4. 删除演练：

```bash
mv src/composite /tmp/topos_legacy_composite_removed
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_nodes tests/test_render_scheduler.py tests/test_tiling.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
mv /tmp/topos_legacy_composite_removed src/composite
```

5. 如果演练通过，正式删除：

```bash
git rm -r src/composite
```

### 完成标准

- `src/composite` 从顶层消失。
- 所有 Composite 真实实现只在 `src/features/composite`。
- 测试不再依赖旧路径。

---

## 9. 阶段 G：`src/ui` 最终拆分

### 当前分类策略

| 当前 `src/ui` 内容 | 目标 |
|---|---|
| `main_window.py` | `src/app/main_window.py` |
| Color 面板 | `src/features/color/ui/` |
| Edit wrapper | 删除或迁到 `features/edit/ui` |
| audio_mixer | 如果只服务 Edit，迁到 `features/edit/ui/audio_mixer`；如果全局，迁到 `shared/ui/audio_mixer` |
| widgets | `src/shared/ui/widgets` |
| preferences / shortcut / welcome dialogs | `src/app/dialogs` 或 `src/shared/ui/dialogs` |
| files/library/info panels | 如果全局应用壳，迁到 `src/app/panels`；如果 Color 专属，迁到 `features/color/ui` |

### 实施步骤

1. 创建目标目录：

```bash
mkdir -p src/shared/ui/widgets src/shared/ui/dialogs src/app/panels src/app/dialogs
```

2. 先迁 widgets：

```bash
git mv src/ui/widgets src/shared/ui/widgets
```

3. 旧路径保留 wrapper 包：

```text
src/ui/widgets/*.py -> src.shared.ui.widgets.*
```

4. 再迁 dialogs/panels。

5. 最后处理 Color UI 面板。

### 验证

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_feature_flags_registry.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from src.app.main_window import MainWindow
print(MainWindow)
PY
```

### 完成标准

- `src/ui` 只剩 wrapper。
- 完成测试迁移后，`src/ui` 可删除。

---

## 10. 阶段 H：`src/core` 最终拆分

### 拆分目标

| 当前路径 | 目标 |
|---|---|
| `src/core/image_engine.py` | `src/features/color/core/image_engine.py` |
| `src/core/async_renderer.py` | `src/features/color/core/async_renderer.py` |
| `src/core/shader_compiler.py` | `src/features/color/core/shader_compiler.py` |
| `src/core/gl_backend.py` | `src/shared/gpu/gl_backend.py` |
| `src/core/media/` | `src/shared/media/` |
| `src/core/video/` | `src/shared/video/` |
| `src/core/edit/` | `src/features/edit/core/` |
| `src/core/export/` | `src/shared/export/` 或 `src/features/deliver/core/` |
| `src/core/project/` | `src/shared/project/` |
| `src/core/save_router.py` | `src/app/save_router.py` 或 `src/shared/project/save_router.py` |
| `src/core/file_io.py` | `src/shared/io/file_io.py` |

### 风险排序

从低风险到高风险：

1. `project/`
2. `export/`
3. `media/`
4. `video/`
5. `file_io.py`
6. `save_router.py`
7. `gl_backend.py`
8. Color core 文件

建议先迁低风险目录，再动 Color core。

### 完成标准

- `src/core` 只剩 wrapper。
- 所有新代码不再 import `src.core.*`。
- 测试迁移完成后，`src/core` 可删除。

---

## 11. 阶段 I：`src/utils` 最终删除

### 当前文件映射建议

```text
src/utils/config_manager.py      -> src/shared/config/config_manager.py
src/utils/database.py            -> src/shared/db/database.py
src/utils/geometry_math.py       -> src/shared/utils/geometry_math.py
src/utils/i18n.py                -> src/shared/i18n/i18n.py
src/utils/icon_manager.py        -> src/shared/assets/icon_manager.py
src/utils/log_manager.py         -> src/shared/logging/log_manager.py
src/utils/paths.py               -> src/shared/paths/paths.py
src/utils/shortcut_manager.py    -> src/shared/ui/shortcut_manager.py
src/utils/style_sheet.py         -> src/shared/ui/style_sheet.py
src/utils/thumbnail_provider.py  -> src/shared/media/thumbnail_provider.py
src/utils/update_checker.py      -> src/app/update_checker.py
```

### 验证

```bash
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_modular_import_boundaries.py -q
```

### 完成标准

- `src/utils` 只剩 wrapper。
- 应用代码不再 import `src.utils.*`。
- 测试迁移完成后，`src/utils` 可删除。

---

## 12. 阶段 J：资源和本地化收口

### Assets

当前：

```text
src/assets/
```

目标：

```text
src/shared/assets/
```

迁移后更新：

- icon loader
- stylesheet loader
- image resource paths
- 打包配置（如果有）

### Locales

当前：

```text
src/locales/
```

目标：

```text
src/shared/i18n/locales/
```

验证：

```bash
.venv/bin/python -m pytest tests/test_config_manager.py -q
```

人工检查：

- 中英文 UI 文案仍能加载。
- 图标仍显示。
- 样式仍生效。

---

## 13. 阶段 K：Import 边界测试升级

### 目标

靠测试阻止旧目录回流。

### 新增或扩展测试

```text
tests/test_src_directory_boundaries.py
```

### 规则

1. `src/features/color` 不允许 import `src.core`、`src.ui`、`src.utils`。
2. `src/features/edit` 不允许 import `src.core.edit`、`src.ui.edit`。
3. `src/features/composite` 不允许 import `src.composite`。
4. `src/features/deliver` 不允许 import 旧 export UI。
5. `src/app` 可以 import `src.features.*.module`，但不能 import feature 内部 deep UI，除非通过 adapter。
6. `src/shared` 不允许 import `src.features`。
7. 新代码不允许新增 `src.core.*`、`src.ui.*`、`src.utils.*`。

### 验证

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

### 完成标准

- 每次迁移后边界测试能指出剩余旧 import。
- 后续 AI 不容易把代码写回旧目录。

---

## 14. 阶段 L：最终删除演练

### 目标

证明 `src/` 顶层只剩：

```text
src/app
src/shared
src/features
src/__init__.py
```

### 删除演练命令

不要直接提交删除，先演练：

```bash
tmpdir=$(mktemp -d /tmp/topos-src-clean-drill.XXXXXX)
mv src/core "$tmpdir/core"
mv src/ui "$tmpdir/ui"
mv src/utils "$tmpdir/utils"
mv src/composite "$tmpdir/composite"

.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_feature_flags_registry.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q

mv "$tmpdir/core" src/core
mv "$tmpdir/ui" src/ui
mv "$tmpdir/utils" src/utils
mv "$tmpdir/composite" src/composite
rmdir "$tmpdir"
```

如果演练通过，再正式删除：

```bash
git rm -r src/core src/ui src/utils src/composite
```

### 完成标准

- 删除旧顶层目录后，核心测试仍通过。
- 应用代码不再引用旧路径。
- `src/` 视觉上接近目标结构。

---

## 15. 推荐提交顺序

不要一个提交完成全部工作。建议提交顺序：

1. `chore: clean generated files from src tree`
2. `refactor: move application shell into app package`
3. `refactor: move shared utils into shared package`
4. `refactor: move media and video foundations into shared`
5. `refactor: move color ui into color feature`
6. `refactor: move color core into color feature`
7. `refactor: move color shaders into color feature`
8. `refactor: move app assets and locales into shared`
9. `test: enforce simplified src directory boundaries`
10. `refactor: remove legacy composite wrappers`
11. `refactor: remove legacy core ui utils wrappers`

每个提交前最少跑：

```bash
.venv/bin/python -m compileall -q src
git diff --check
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_feature_flags_registry.py -q
```

涉及 Color 时额外跑：

```bash
.venv/bin/python -m pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_color_tools_params.py -q
```

涉及 Composite 时额外跑：

```bash
.venv/bin/python -m pytest tests/test_nodes tests/test_render_scheduler.py tests/test_tiling.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
```

涉及 Media/Video 时额外跑：

```bash
.venv/bin/python -m pytest tests/media tests/test_video_upload_command.py tests/test_video_pipeline_decision.py -q
```

---

## 16. 当前最推荐的下一步

下一位执行者不要直接搬 Color core。推荐先做：

```text
阶段 A -> 阶段 B -> 阶段 I 的 config/i18n/logging 小文件 -> 阶段 K 边界测试
```

原因：

- 风险低。
- 能快速让 `src/` 顶层变干净。
- 能建立边界测试，防止后续大迁移回流。
- 不触碰调色核心 GPU 管线。

第一轮执行范围建议：

```text
1. 清理 src 下生成文件和数据库/config 放置问题。
2. 将 src/ui/main_window.py 迁到 src/app/main_window.py，旧路径 wrapper。
3. 将 src/utils/config_manager.py、i18n.py、log_manager.py、paths.py 迁到 shared。
4. 新增 tests/test_src_directory_boundaries.py。
5. 不动 image_engine.py / async_renderer.py / shader_compiler.py / gl_backend.py。
```

---

## 17. 给后续 AI 的执行提示词

```text
你在 Topos Color 项目中执行 src 目录极简化。

目标结构：
src/
  app/
  shared/
  features/

请严格按照 docs/src_directory_simplification_plan_2026-05-26.md 执行。

约束：
1. 每次只做一个阶段，不要一次性大搬家。
2. Color 调色核心是最高风险区域，第一轮不要动：
   - src/core/image_engine.py
   - src/core/async_renderer.py
   - src/core/shader_compiler.py
   - src/core/gl_backend.py
3. 所有旧路径先改 wrapper，再迁测试，再删除。
4. 每阶段必须运行计划里的验证命令。
5. src/features/* 不允许反向依赖旧 src/core/src/ui/src/utils。
6. src/shared 不允许依赖 src/features。

本轮推荐只执行：
- 阶段 A：清理生成文件和源码目录里的本地数据
- 阶段 B：MainWindow 和 main 进入 src/app
- 阶段 I 的 config/i18n/logging/paths 小文件迁移
- 阶段 K：新增边界测试

完成后输出：
- 修改文件列表
- wrapper 列表
- 删除/迁移列表
- 验证命令和结果
- 剩余旧目录清单
```

---

## 18. 瘦身完成后的 Composite 修复计划

> 执行时机：只有当 `src/` 顶层瘦身完成，并且旧 `src/core`、`src/ui`、`src/utils`、`src/composite` 已经删除或完成删除演练后，才开始本阶段。

本阶段不要和目录迁移混在同一个提交里。目录瘦身期间只保证 Composite 可导入、可禁用、可删除演练通过；真正的 Composite 功能修复放到这里集中做。

### 18.1 当前已知 Composite 状态

已经完成：

- Composite 真实实现位于 `src/features/composite/`。
- `CompositeRuntime` 和 `MainWindowAdapter` 已建立。
- `src/features/composite` 不再依赖旧 `src.composite`。
- `TOPOS_FEATURE_COMPOSITE=0` 时，删除 Composite 目录演练已通过。
- `tests/test_nodes`、`tests/test_render_scheduler.py`、`tests/test_tiling.py`、`tests/test_composite_snapshot.py`、`tests/test_composite_node_contract.py` 已能作为基础回归。

仍需修复或确认：

- Composite 节点 UI 与 Viewer 的真实交互还需要人工验收：新增节点、连线、选择节点、属性面板同步、viewer overlay、缩略图刷新。
- `CompositeRuntime` 目前主要负责创建和生命周期，未来可继续吸收 MainWindow 中的 Composite 保存、导出、页面切换细节。
- AI / Text / Roto 等节点中部分仍是 placeholder 或轻量实现，需要确认产品预期。
- Composite 全分辨率导出路径需要和 `features/deliver` / `shared/export` 的新结构对齐。
- 旧测试仍可能大量使用 `src.composite.*`，当瘦身完成并删除旧 wrapper 后，需要统一迁到新路径。

### 18.2 修复顺序

#### S1：Composite 页面基础交互验收

目标：

- Composite 页面能打开。
- Node Graph 显示正常。
- Viewer 能显示 Color 输出。
- Properties Panel 能跟随节点选择刷新。
- Performance Panel 不阻塞页面创建。

验证命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.features.composite.runtime import CompositeRuntime

app = QApplication.instance() or QApplication(sys.argv)
runtime = CompositeRuntime()
assert runtime.create(None)
assert runtime.is_available()
assert runtime.page is not None
assert runtime.viewer is not None
assert runtime.properties_panel is not None
runtime.cleanup()
print("CompositeRuntime smoke OK")
PY
```

人工验收：

```text
1. 启动应用。
2. 切到 Composite 页面。
3. 确认 Node Graph、Viewer、Properties、Performance 面板都出现。
4. 从 Color 页面返回再进入 Composite，确认 Viewer swap 正常。
5. 关闭应用，确认没有 renderer/thread cleanup 报错。
```

完成标准：

- 页面打开稳定。
- 进入/离开 Composite 不影响 Color 页面。
- 关闭应用无明显资源释放错误。

#### S2：节点创建、连线、属性同步修复

目标：

- Node search / node menu 可以创建节点。
- 节点拖动、选择、删除、复制粘贴正常。
- 连接线创建/断开正常。
- 属性面板修改参数后，NodeGraph 和 renderer 都收到更新。
- Undo/Redo 对节点操作有效。

重点文件：

```text
src/features/composite/ui/composite_page.py
src/features/composite/ui/node_editor_widget.py
src/features/composite/ui/node_editor_actions.py
src/features/composite/ui/node_graph_commands.py
src/features/composite/ui/node_properties_panel.py
src/features/composite/core/node_engine.py
src/features/composite/core/undo_stack.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_node_graph_commands.py tests/test_composite_undo.py tests/test_composite_graph.py -q
```

人工验收：

```text
1. 新建 Grade、Blur、Merge、Transform、Output 节点。
2. 连接 Color -> Grade -> Blur -> Output。
3. 修改 Blur radius，确认 Viewer 刷新。
4. 删除 Blur，Undo 恢复，Redo 再删除。
5. 保存项目后重新打开，节点位置和连接还原。
```

完成标准：

- 基础节点编辑无异常。
- 参数改变能触发 dirty/evaluate。
- undo/redo 不破坏 graph。

#### S3：Viewer / Overlay / Handle 修复

目标：

- CompositeViewer 显示渲染结果。
- metadata overlay 的 format/bbox 正确显示。
- Transform / CornerPin / Mask / Roto handles 可交互。
- Viewer 工具状态不和 Color Canvas 冲突。

重点文件：

```text
src/features/composite/ui/composite_viewport.py
src/features/composite/ui/viewer_tool_system.py
src/features/composite/ui/viewer_handles.py
src/features/composite/ui/viewer_handle_drawing.py
src/features/composite/adapters/main_window_adapter.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_viewer_tool_system.py tests/test_viewer_selection_system.py tests/test_composite_mask_roto_handles.py -q
```

人工验收：

```text
1. Transform 节点选中后，Viewer handles 出现。
2. 拖动 handle 后参数更新。
3. Mask/Roto 节点 handle 显示和拖动正常。
4. Viewer zoom/pan 不影响 Node Graph。
5. 切回 Color 页面后 Canvas 恢复。
```

完成标准：

- Viewer 不黑屏。
- handles 不漂移、不错位。
- overlay metadata 正常。

#### S4：渲染调度、缓存、tiling 回归

目标：

- RenderScheduler 计划正确。
- Dirty propagation 正确。
- Cache hit/miss 合理。
- Tiling 在 4K/8K/export 下选择合理。
- CompositeRenderer 和 CompositeExportRenderer 使用同一套 graph snapshot 逻辑。

重点文件：

```text
src/features/composite/render/render_scheduler.py
src/features/composite/render/composite_renderer.py
src/features/composite/render/composite_export_renderer.py
src/features/composite/render/cache_manager.py
src/features/composite/render/gpu_resource_pool.py
src/features/composite/core/graph_snapshot.py
src/features/composite/core/tile_scheduler.py
src/features/composite/core/tile_padding_computer.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_render_scheduler.py tests/test_parallel_evaluation.py tests/test_tiling.py tests/test_composite_tile.py tests/test_dirty_propagation.py tests/test_cache_strategy.py tests/test_composite_cache_key.py -q
```

完成标准：

- 调度/缓存/tiling 测试全部通过。
- 4K/8K 不因 tile size 过大导致估算内存异常。
- Dirty 节点和缓存节点集合符合预期。

#### S5：Composite 节点功能补齐

目标：

按产品优先级补齐节点，不在一个提交里做完所有节点。

优先级建议：

1. 基础输入输出：
   - Color input
   - Image input
   - Video frame
   - Output
   - File output
2. 常用处理：
   - Grade
   - Blur
   - Transform
   - Merge
   - Crop
   - Reformat
3. Alpha / channel：
   - Premult / Unpremult
   - Shuffle / CopyChannel / SetAlpha
4. Keyer / matte：
   - LumaKeyer
   - ChromaKeyer
   - DifferenceKeyer
   - MatteCleanup
5. Generator / mask：
   - Constant
   - Noise
   - Ramp
   - Mask
   - RotoShape
6. Advanced / placeholder：
   - Text
   - AI Generate / Inpaint / Expand / Remove / Denoise

验证：

```bash
.venv/bin/python -m pytest tests/test_nodes tests/test_composite_nodes_input.py tests/test_composite_nodes_color.py tests/test_composite_nodes_merge.py tests/test_composite_nodes_transform.py tests/test_composite_nodes_keyer.py tests/test_composite_nodes_mask.py tests/test_composite_nodes_ai.py -q
```

完成标准：

- 所有非 placeholder 节点有明确 evaluate 行为。
- placeholder 节点有明确 UI 标识，不假装可用。
- 节点序列化/反序列化不丢参数。

#### S6：Composite 保存、加载、项目集成

目标：

- Composite graph 能保存到项目。
- Viewer state 能保存。
- ImageInput 相对路径能正确恢复。
- Edit -> Composite 的 `composite_id` 跳转正常。
- 删除 Composite feature 时，项目仍能以降级方式打开。

重点文件：

```text
src/features/composite/adapters/main_window_adapter.py
src/features/composite/runtime.py
src/features/edit/core/composite_graph_data.py
src/shared/project/
src/app/main_window.py
```

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_composite_graph_data.py tests/test_composite_project_migration.py tests/test_page_context.py -q
```

人工验收：

```text
1. 创建 Composite graph。
2. 保存项目。
3. 关闭应用并重新打开项目。
4. 确认节点、连接、位置、viewer state 还原。
5. 从 Edit clip 进入对应 composite_id。
```

完成标准：

- 保存/加载不丢 graph。
- 旧项目格式能迁移或优雅降级。
- 缺失节点显示 UnknownPlaceholder，不导致项目打不开。

#### S7：Composite 导出和 Deliver 集成

目标：

- Composite 输出可作为 Deliver export source。
- Quick export 和 full-res export 都能处理 Composite graph。
- ExportRenderer 与新的 `shared/export` / `features/deliver` 结构对齐。

重点文件：

```text
src/features/composite/render/composite_export_renderer.py
src/features/deliver/
src/shared/export/
src/app/main_window.py
```

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_timeline_export.py tests/test_composite_snapshot.py tests/test_tiling.py -q
```

人工验收：

```text
1. Composite graph 输出一帧。
2. Deliver 选择 Composite output。
3. Quick export 成功。
4. Full-res export 成功。
5. 导出结果尺寸、alpha、bbox 符合 Output 节点设置。
```

完成标准：

- Composite export 不再依赖旧 `src.composite`。
- Deliver 能识别 composite output。
- full-res readback 稳定。

#### S8：Composite 禁用 / 删除最终回归

目标：

确认 Composite 修复完成后，仍然保持模块化边界。

验证：

```bash
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m compileall -q src
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m pytest tests/test_config_manager.py tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_feature_flags_registry.py -q
```

删除演练：

```bash
tmpdir=$(mktemp -d /tmp/topos-composite-final-drill.XXXXXX)
mv src/features/composite "$tmpdir/composite"

TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m compileall -q src
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m pytest tests/test_config_manager.py tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_feature_flags_registry.py -q

mv "$tmpdir/composite" src/features/composite
rmdir "$tmpdir"
```

完成标准：

- Composite 修复没有重新引入跨 feature 强依赖。
- 关闭 Composite 后 Color/Edit/Deliver 仍可用。
- 删除 Composite feature 目录演练通过。

### 18.3 Composite 修复提交建议

建议提交顺序：

1. `test: add composite ui smoke coverage`
2. `fix: stabilize composite node graph editing`
3. `fix: stabilize composite viewer tools`
4. `fix: harden composite render scheduling`
5. `fix: complete priority composite nodes`
6. `fix: restore composite project save load`
7. `fix: wire composite export through deliver`
8. `test: verify composite disabled deletion drill`

每个提交前至少跑：

```bash
.venv/bin/python -m compileall -q src
git diff --check
.venv/bin/python -m pytest tests/test_modular_import_boundaries.py tests/test_src_directory_boundaries.py -q
.venv/bin/python -m pytest tests/test_nodes tests/test_render_scheduler.py tests/test_tiling.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
```

### 18.4 给后续 AI 的 Composite 修复提示词

```text
你现在开始执行 Composite 功能修复。

前置条件：
- src/ 目录瘦身已经完成。
- src 顶层只剩 app/shared/features。
- Composite 真实代码位于 src/features/composite。
- 旧 src.composite wrapper 已删除或不再被应用代码依赖。

请严格按照 docs/src_directory_simplification_plan_2026-05-26.md 的“阶段 18：瘦身完成后的 Composite 修复计划”执行。

约束：
1. 不要修改 Color 核心调色管线，除非 Composite 输入 ColorOutput 必须接线。
2. 不要重新引入 src.composite、src.core、src.ui、src.utils 旧路径 import。
3. 每次只修一个 Composite 子系统：UI、Viewer、Render、Nodes、Save/Load、Export。
4. 每个修复提交前运行计划中的验证命令。
5. 修复后必须保持 TOPOS_FEATURE_COMPOSITE=0 删除演练通过。

输出：
- 修改文件列表
- 修复的 Composite 子系统
- 验证命令和结果
- 是否仍能禁用/删除 Composite
```

---

## 19. 最终验收清单

全部完成后，必须满足：

- `find src -maxdepth 1 -type d` 只看到：

```text
src
src/app
src/shared
src/features
```

- `src/core` 不存在。
- `src/ui` 不存在。
- `src/utils` 不存在。
- `src/composite` 不存在。
- `src/assets` 不存在。
- `src/locales` 不存在。
- `src/scripting` 不存在或已明确迁入 `features/scripting` / `shared/scripting`。
- `src/config.json` 不存在。
- `src/*.db` 不存在。
- `src/features/color/core` 承载 Color 核心。
- `src/features/color/ui` 承载 Color UI。
- `src/features/color/shaders` 承载 Color shader。
- `src/shared` 不依赖任何 feature。
- `src/app` 不直接 import feature 内部 UI，只通过 module/runtime/adapter。
- 关闭任意非 Color feature flag 时，其它功能测试仍通过。
- 删除 Composite feature 目录演练仍通过。
- Color 调色回归测试通过。

最终验证命令：

```bash
.venv/bin/python -m compileall -q src
git diff --check
.venv/bin/python -m pytest tests/test_config_manager.py tests/test_feature_flags_registry.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
.venv/bin/python -m pytest tests/test_modular_import_boundaries.py tests/test_src_directory_boundaries.py -q
.venv/bin/python -m pytest tests/test_nodes tests/test_render_scheduler.py tests/test_tiling.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m pytest tests/test_config_manager.py tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_feature_flags_registry.py -q
```
