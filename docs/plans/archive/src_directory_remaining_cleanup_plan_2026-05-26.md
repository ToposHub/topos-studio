# Src 目录剩余瘦身与验收修复计划（2026-05-26）

> 适用状态：上一轮“全部优化”已经完成后，本次验收发现项目还没有达到最终目标结构。本计划只保留当前仍需优化和修复的内容，已经完成的内容不再展开。

目标最终形态仍然是：

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
    composite/
      core/
      ui/
      nodes/
    deliver/
      core/
      ui/
```

## 1. 本次验收结论

结论：**还没有完全完成**。

目前已经有明显进展：

- `src/app`、`src/shared`、`src/features` 已经建立。
- `src/features/color`、`src/features/edit`、`src/features/composite`、`src/features/deliver` 都已经存在。
- 顶层 `src/composite` 已经不存在，Composite 旧顶层包清理这一步基本完成。
- `src/ui/main_window.py` 已经是 1 行兼容 wrapper，真实实现位于 `src/app/main_window.py`。
- `python3 -m compileall -q src` 通过，没有语法错误。
- `python3 -m pytest tests/test_modular_import_boundaries.py -q` 通过，Composite 旧路径没有明显回流。

但离最终目标还有几类关键问题：

- `src/core` 和 `src/ui` 仍然存在，且 `git ls-files src/core src/ui | wc -l` 显示仍有 **180 个被跟踪文件**。
- `src/core`、`src/ui` 中仍有大量真实实现，不只是 wrapper。
- `src/app/main_window.py` 仍大量 import `src.core.*`、`src.ui.*`，应用壳还没有真正瘦下来。
- `src/features/color` 仍有大量旧路径 import，`tests/test_src_directory_boundaries.py` 报告 Color 侧还有 **72 个 legacy imports**。
- `src/shared` 仍直接 import `src.features.*`，边界方向不干净。
- `src/` 顶层仍有运行产物或非目标文件：`.DS_Store`、`.images_cache`、`__pycache__`、`config.json`、`metadata.db`、`topos.db`、`luts`、`main.py`、`core`、`ui`。
- 当前工作区还有未提交改动，后续执行前需要先确认这些改动是否作为一个检查点提交。

## 2. 本次验收命令与结果

### 2.1 目录检查

命令：

```bash
find src -maxdepth 1 -mindepth 1 -print | sort
```

当前结果包含：

```text
src/.DS_Store
src/.images_cache
src/__init__.py
src/__pycache__
src/app
src/config.json
src/core
src/features
src/luts
src/main.py
src/metadata.db
src/shared
src/topos.db
src/ui
```

验收判断：未达到“顶层只保留 app/shared/features”的目标。

### 2.2 旧目录文件数量

命令：

```bash
git ls-files src/core src/ui | wc -l
find src/core src/ui -type f -name "*.py" | wc -l
```

结果：

```text
180
180
```

验收判断：旧目录仍是主要代码承载区之一，不能删除。

### 2.3 编译检查

命令：

```bash
python3 -m compileall -q src
```

结果：通过。

注意：`src/features/color/ui/canvas.py` 有两个 `SyntaxWarning: invalid escape sequence '\)'`，不是阻断项，但后续应顺手修掉。

### 2.4 Composite 边界检查

命令：

```bash
python3 -m pytest tests/test_modular_import_boundaries.py -q
```

结果：

```text
8 passed
```

验收判断：Composite 旧顶层路径没有明显回流，这是本轮完成得比较好的部分。

### 2.5 Src 目录边界检查

命令：

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

结果：

```text
1 failed, 8 passed, 3 warnings
```

失败项：

```text
test_app_does_not_deep_import_feature_internals
src/app/main_window.py:686 'from src.features.scripting.engine import ScriptEngine'
```

警告项：

- `src/features/color` 仍有 72 个 `src.core` / `src.ui` / `src.utils` 旧路径 import。
- `src/shared` 仍有 12 个 `src.features` import。
- `src/` 顶层仍有 `ui`、`core` 两个旧目录。

### 2.6 基础功能测试

命令：

```bash
.venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py -q
```

结果：

```text
3 failed, 89 passed
```

失败项：

- `tests/test_page_context.py::TestPageContextIntegration::test_switch_to_page_with_context`
  - 失败原因：测试使用 `MainWindow.switch_to_page_with_context.__func__`，但当前类方法属性已经是普通 function，没有 `__func__`。
  - 判断：这更像测试绑定方式过时，不应为了测试把生产函数改成 `classmethod`。
- `tests/test_page_context.py::TestExportPanelMethods::test_set_export_source`
- `tests/test_page_context.py::TestExportPanelMethods::test_set_timeline_id`
  - 失败原因：测试用 `ExportPanel.__new__(ExportPanel)` 绕过 Qt 初始化，生产代码里 `hasattr(self, "_source_label")` / `hasattr(self, "_timeline_label")` 触发 PyQt 未初始化对象的 `RuntimeError`。
  - 判断：可以在生产代码里改成不触发 Qt 属性访问的安全检查。

### 2.7 系统 Python 环境测试

命令：

```bash
python3 -m pytest tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py -q
```

结果：收集阶段失败，缺少 `cv2` 和 `moderngl`。

判断：这是环境问题。后续验收统一使用 `.venv/bin/python`。

## 3. 当前必须优先处理的问题清单

| ID | 优先级 | 问题 | 当前证据 | 修复目标 |
|---|---:|---|---|---|
| R-001 | P0 | 边界测试仍有 1 个失败 | `src/app/main_window.py` deep import `src.features.scripting.engine` | App 只通过 feature module/runtime/adapter 接触 scripting |
| R-002 | P0 | `tests/test_page_context.py` 有 3 个失败 | `.venv` 下基础测试 `3 failed` | 先把红灯测试修绿，避免后续瘦身扩大不确定性 |
| R-003 | P1 | `src/core`、`src/ui` 仍占据顶层 | 180 个 tracked py 文件 | 旧目录最终消失，或至少先变成纯 wrapper 再删除 |
| R-004 | P1 | Color feature 仍反向依赖旧路径 | 72 个 legacy imports | `src/features/color` 不再 import `src.core` / `src.ui` / `src.utils` |
| R-005 | P1 | Shared 反向 import Features | 12 个 `src.shared -> src.features` import | Shared 只依赖 shared/app 协议，不直接依赖具体 feature |
| R-006 | P1 | App shell 仍混合业务 UI 和核心实现 | `src/app/main_window.py` 仍大量 import old core/ui | MainWindow 只负责应用壳和 feature runtime 编排 |
| R-007 | P1 | 顶层运行产物污染源码目录 | `.DS_Store`、`.images_cache`、`.db`、`config.json` 等 | 运行产物离开 `src/`，配置进入 `config/` 或 `shared/config` |
| R-008 | P2 | 边界测试目前部分只是 warning | Color legacy import、shared feature import、top-level core/ui 只是 warning | 迁移完成后将 warning 转成 assert |
| R-009 | P2 | Composite 修复尚未开始完整交互验收 | 目前只确认 import/boundary 基础通过 | 目录瘦身完成后，再集中修 Composite UI/Viewer/Render/Export |

## 4. 执行总原则

1. **先修红灯，再继续大迁移。**
   当前 `tests/test_src_directory_boundaries.py` 和 `tests/test_page_context.py` 还有失败项。先修这些小红灯，后续每一步才容易判断是否引入新问题。

2. **Color 调色核心只做迁移和 import 改线，不改算法。**
   以下文件是核心调色管线，除非本阶段明确要求，否则不要顺手改内部逻辑：
   - `src/features/color/core/image_engine.py`
   - `src/features/color/core/async_renderer.py`
   - `src/features/color/core/shader_compiler.py`
   - `src/shared/gpu/gl_backend.py` 或旧 `src/core/gl_backend.py`

3. **每一步都先保留 wrapper，再删除旧目录。**
   直接删除 `src/core` 或 `src/ui` 风险太高。正确节奏是：
   - 真实实现迁到目标目录。
   - 旧路径变成 1-2 行 wrapper。
   - 所有 app/features/shared/tests 改用新路径。
   - 做删除演练。
   - 最后删除 wrapper 目录。

4. **不要把 Composite 功能修复混入 src 瘦身提交。**
   当前目标是让结构变干净。Composite 功能修复放在最后单独做。

5. **每个阶段单独提交。**
   不要一个提交同时包含测试修复、Color import 迁移、shared 边界重构、Composite 功能修复。

## 5. 阶段 0：建立干净检查点

### 目标

确认当前工作区改动归属，避免下一轮 AI 把已有改动和新改动混在一起。

### 操作

1. 查看当前改动：

```bash
git status --short
git diff --stat
```

2. 当前已知未提交文件包括：

```text
src/app/main_window.py
src/core/video/session.py
src/features/color/ui/tools_panel.py
src/features/scripting/api.py
src/features/scripting/engine.py
src/main.py
src/shared/ai_models/onnx_env.py
src/ui/audio_mixer/audio_mixer_panel.py
src/ui/audio_mixer/track_mixer_widget.py
src/ui/effect_degradation_indicator.py
src/ui/files_panel.py
src/ui/info_panel.py
src/ui/library_panel.py
src/ui/playback_hud.py
src/ui/preferences_dialog.py
src/ui/resize_dialog.py
src/ui/script_panel.py
src/ui/shortcut_dialog.py
src/ui/video_control_panel.py
src/ui/welcome_dialog.py
src/ui/widgets/curves_widget.py
src/ui/widgets/drag_value.py
src/ui/widgets/hsl_curves_widget.py
src/ui/widgets/lut_gallery.py
src/ui/widgets/vectorscope_widget.py
```

3. 如果这些改动是上一轮优化结果，建议先提交：

```bash
git add src
git commit -m "refactor: continue src modular import cleanup"
```

4. 如果准备先修红灯，也可以把阶段 1 修复放进同一个提交，但不要混入大规模目录移动。

### 验证

```bash
git status --short
```

验收标准：

- 要么工作区干净。
- 要么只剩当前阶段明确要处理的文件。

## 6. 阶段 1：先修当前红灯测试

### 1.1 修复 App 对 scripting 的 deep import

当前问题：

```python
from src.features.scripting.engine import ScriptEngine
```

位置：

```text
src/app/main_window.py
```

这个 import 违反了 App 只通过 feature module/runtime/adapter 接触 feature 的规则。

推荐修复：

1. 新建：

```text
src/features/scripting/module.py
```

2. 在 module 中提供稳定入口：

```python
from __future__ import annotations

from src.features.scripting.engine import ScriptEngine


class ScriptingModule:
    def is_available(self) -> bool:
        return True

    def create_engine(self, image_engine):
        return ScriptEngine(image_engine)


def create_script_engine(image_engine):
    return ScriptingModule().create_engine(image_engine)
```

3. 修改 `src/app/main_window.py`：

```python
from src.features.scripting.module import create_script_engine

self.script_engine = create_script_engine(self.engine)
```

4. 修改边界测试 allowlist，让 App 允许 import `src.features.scripting.module`，但不允许 import `src.features.scripting.engine` / `api`。

### 验证

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

验收标准：

- `test_app_does_not_deep_import_feature_internals` 通过。
- App 不再 import `src.features.scripting.engine`。

额外检查：

```bash
rg -n "from src\.features\.scripting\.(engine|api)|import src\.features\.scripting\.(engine|api)" src/app tests -g "*.py"
```

验收标准：没有结果，或者只有明确允许的测试注释。

### 1.2 修复 `switch_to_page_with_context` 测试绑定方式

当前失败：

```text
AttributeError: 'function' object has no attribute '__func__'
```

位置：

```text
tests/test_page_context.py
```

推荐修复测试，不要为了测试把生产方法改成 `classmethod`。

将：

```python
method = MainWindow.switch_to_page_with_context.__func__
bound_method = method.__get__(mock_main_window, MainWindow)
```

改为：

```python
method = MainWindow.switch_to_page_with_context
bound_method = method.__get__(mock_main_window, MainWindow)
```

### 验证

```bash
.venv/bin/python -m pytest tests/test_page_context.py::TestPageContextIntegration::test_switch_to_page_with_context -q
```

验收标准：该测试通过。

### 1.3 修复 ExportPanel 未初始化对象上的 `hasattr`

当前失败：

```text
RuntimeError: super-class __init__() of type ExportPanel was never called
```

位置：

```text
src/shared/export/export_panel.py
```

问题代码：

```python
if hasattr(self, '_source_label'):
    ...

if hasattr(self, '_timeline_label'):
    ...
```

PyQt 对象在 `__new__` 但未 `__init__` 时，`hasattr` 可能触发 Qt 层属性访问并抛错。

推荐改法：

```python
source_label = self.__dict__.get("_source_label")
if source_label is not None:
    source_label.setText(...)

timeline_label = self.__dict__.get("_timeline_label")
if timeline_label is not None:
    timeline_label.setText(...)
```

如果担心某些 PyQt 对象没有普通 `__dict__`，可写成小工具函数：

```python
def _instance_attr(obj, name: str):
    try:
        return object.__getattribute__(obj, "__dict__").get(name)
    except Exception:
        return None
```

不要使用 `getattr(self, "_source_label", None)`，它仍可能触发 PyQt 属性访问。

### 验证

```bash
.venv/bin/python -m pytest tests/test_page_context.py::TestExportPanelMethods -q
```

验收标准：两个 ExportPanel 测试通过。

### 阶段 1 总体验证

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py -q
python3 -m compileall -q src
```

验收标准：

- `tests/test_src_directory_boundaries.py` 不再失败。
- `tests/test_page_context.py` 不再失败。
- `compileall` 通过。

建议提交：

```bash
git add src/features/scripting/module.py src/app/main_window.py src/shared/export/export_panel.py tests/test_src_directory_boundaries.py tests/test_page_context.py
git commit -m "test: restore modular boundary smoke checks"
```

## 7. 阶段 2：清理 `src/` 顶层运行产物和配置文件

### 目标

先清理不该出现在源码目录的非代码文件，让 `src/` 视觉上明显变干净。

### 当前待处理

```text
src/.DS_Store
src/.images_cache
src/__pycache__
src/config.json
src/metadata.db
src/topos.db
src/luts
src/main.py
```

### 操作

1. 确认这些文件是否被 git 跟踪：

```bash
git ls-files src/config.json src/metadata.db src/topos.db src/.DS_Store src/.images_cache src/luts
```

本次验收结果为空，说明它们当前不是 tracked 文件。

2. 删除空目录和本地运行产物：

```bash
rm -rf src/.DS_Store src/.images_cache src/__pycache__ src/luts
rm -f src/metadata.db src/topos.db
```

3. 处理 `src/config.json`：

先查读取位置：

```bash
rg -n "config\.json|src/config|ConfigManager" src tests -g "*.py"
```

推荐目标：

```text
config/default_config.json
```

或：

```text
src/shared/config/default_config.json
```

如果是用户运行态配置，不要放仓库，改为用户目录或项目根 `.topos/`。

4. 处理 `src/main.py`：

短期可以保留 wrapper，但最终 `src/` 只保留包代码时，应改为根目录入口或 package entry point。

推荐过渡：

```text
main.py                  # 项目根入口
src/app/main.py          # 真实启动逻辑
src/main.py              # 临时 wrapper，一轮后删除
```

最终删除：

```text
src/main.py
```

### 验证

```bash
find src -maxdepth 1 -mindepth 1 -print | sort
git status --short
python3 -m compileall -q src
```

验收标准：

- `src/.DS_Store`、`src/.images_cache`、`src/__pycache__`、`src/metadata.db`、`src/topos.db`、空 `src/luts` 不再出现。
- 如果 `src/config.json` 已迁移，应用启动和配置读取仍正常。

## 8. 阶段 3：把 `src/app/main_window.py` 瘦成真正应用壳

### 当前问题

`src/app/main_window.py` 仍然直接 import 大量旧路径：

```text
src.core.image_engine
src.core.file_io
src.core.plugin_manager
src.ui.canvas
src.ui.resize_dialog
src.ui.shortcut_dialog
src.ui.library_panel
src.ui.files_panel
src.ui.info_panel
src.ui.layer_panel
src.ui.denoise_panel
src.ui.deblur_panel
src.ui.mixer_panel
src.ui.audio_mixer
src.ui.warper_panel
src.ui.blur_sharpen_panel
src.ui.film_grain_panel
src.ui.crop_panel
src.ui.scope_panel
src.ui.plugin_panel
src.ui.presets_panel
src.core.layer
src.core.video_decoder
src.core.video_playback
...
```

这说明 App 还不是“应用壳”，它仍直接知道 Color/Edit/Deliver/媒体层的内部实现。

### 目标

MainWindow 只做：

- 菜单、窗口、dock 布局。
- 页面切换。
- 调用 feature module/runtime 暴露的接口。
- 统一 AppContext 注入。

MainWindow 不直接 import：

- Color 面板细节。
- Edit 面板细节。
- Composite 面板细节。
- Deliver/export UI 细节。
- 低层 media/video/core 细节。

### 推荐拆分

新增或补齐：

```text
src/features/color/runtime.py
src/features/color/adapters/main_window_adapter.py
src/features/edit/runtime.py
src/features/edit/adapters/main_window_adapter.py
src/features/deliver/runtime.py
src/features/deliver/adapters/main_window_adapter.py
src/features/scripting/module.py
```

每个 feature runtime 负责：

- 创建自己内部 UI。
- 连接自己内部 signals。
- 暴露必要的 app-facing methods。
- 接收 AppContext。

App 侧只保留：

```python
from src.features.color.module import ColorModule
from src.features.edit.module import EditModule
from src.features.composite.module import CompositeModule
from src.features.deliver.module import DeliverModule
from src.features.scripting.module import ScriptingModule
```

### 验证

```bash
rg -n "from src\.(core|ui)|import src\.(core|ui)" src/app -g "*.py"
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_page_context.py -q
```

验收标准：

- `src/app` 不再 import `src.core.*` / `src.ui.*`。
- `src/app` import `src.features.*` 时只走 `.module` / `.runtime` / `.adapters` 中允许的入口。
- MainWindow 仍能创建 Color/Edit/Composite/Deliver 页面。

建议提交：

```bash
git add src/app src/features tests/test_src_directory_boundaries.py
git commit -m "refactor: route app shell through feature modules"
```

## 9. 阶段 4：Color 迁移收口（核心管线稳定优先）

### 当前问题

`tests/test_src_directory_boundaries.py` 报告：

```text
src/features/color still has 72 legacy imports
```

典型旧路径：

```text
src.features.color.core.image_engine -> src.core.history / layer / gl_backend / shader_compiler / async_renderer
src.features.color.core.async_renderer -> src.core.gl_backend / media / video_texture_pool / yuv_gpu_upload
src.features.color.ui.canvas -> src.ui.gl_viewport / src.core.gl_backend / src.core.layer
src.features.color.ui.tools_panel -> src.ui.mixer_panel / warper_panel / blur_sharpen_panel / scope_panel
```

### 目标

`src/features/color` 内部不再 import：

```text
src.core.*
src.ui.*
src.utils.*
```

所有依赖改为：

```text
src.features.color.core.*
src.features.color.ui.*
src.features.color.shaders.*
src.shared.gpu.*
src.shared.media.*
src.shared.video.*
src.shared.ui.*
src.shared.config.*
src.shared.i18n.*
```

### 操作顺序

#### 4.1 锁定 Color 核心基线

先跑：

```bash
.venv/bin/python -m pytest tests/test_color_session.py tests/test_color_tools_params.py tests/test_color_tools_lut_exporter.py tests/media/test_color_convert.py tests/media/test_color_metadata_mapping.py -q
python3 -m compileall -q src/features/color src/shared src/app
```

如果本地 GPU/GUI 环境可用，再做手动验收：

1. 打开图片。
2. 调整 exposure/contrast/saturation。
3. 调整 curves/HSL curves。
4. 开关 LUT。
5. 确认 viewer 刷新、参数保存、撤销恢复正常。

#### 4.2 只改 import，不改算法

优先处理 wrapper 已经存在的文件：

```text
src/core/image_engine.py       -> src/features/color/core/image_engine.py
src/core/async_renderer.py     -> src/features/color/core/async_renderer.py
src/core/shader_compiler.py    -> src/features/color/core/shader_compiler.py
src/core/color_manager.py      -> src/features/color/core/color_manager.py
src/core/layer.py              -> src/features/color/core/layer.py
src/ui/canvas.py               -> src/features/color/ui/canvas.py
```

对这些文件中的 import 做逐项替换：

```text
src.core.history               -> src.features.color.core.history
src.core.layer                 -> src.features.color.core.layer
src.core.cache_manager         -> src.features.color.core.cache_manager
src.core.color_manager         -> src.features.color.core.color_manager
src.core.color_output          -> src.features.color.core.color_output
src.core.shader_compiler       -> src.features.color.core.shader_compiler
src.core.shader_nodes          -> src.features.color.core.shader_nodes
src.core.shaders               -> src.features.color.shaders.shaders 或明确 shader 模块
src.core.plugin_manager        -> src.features.color.core.plugin_manager
src.core.denoise_engine        -> src.features.color.core.denoise_engine
src.core.gl_backend            -> src.shared.gpu.gl_backend
```

如果目标文件还不存在，先 `git mv` 真实实现，再在旧路径留下 wrapper。

#### 4.3 UI 面板迁移

Color UI 面板统一放到：

```text
src/features/color/ui/
```

旧 `src/ui/*.py` 只允许存在 1-2 行 wrapper。

典型映射：

```text
src/ui/mixer_panel.py          -> src/features/color/ui/mixer_panel.py
src/ui/warper_panel.py         -> src/features/color/ui/warper_panel.py
src/ui/blur_sharpen_panel.py   -> src/features/color/ui/blur_sharpen_panel.py
src/ui/scope_panel.py          -> src/features/color/ui/scope_panel.py
src/ui/crop_panel.py           -> src/features/color/ui/crop_panel.py
src/ui/plugin_panel.py         -> src/features/color/ui/plugin_panel.py
src/ui/presets_panel.py        -> src/features/color/ui/presets_panel.py
```

共享 widgets 统一放到：

```text
src/shared/ui/widgets/
```

旧 `src/ui/widgets/*.py` 只保留 wrapper，最终删除。

#### 4.4 验证

```bash
rg -n "from src\.(core|ui|utils)|import src\.(core|ui|utils)" src/features/color -g "*.py"
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_color_session.py tests/test_color_tools_params.py tests/test_color_tools_lut_exporter.py -q
python3 -m compileall -q src
```

验收标准：

- `src/features/color` 不再出现旧路径 import。
- Color 相关测试通过。
- 手动调色基本路径正常。
- 没有修改核心调色算法行为。

建议提交：

```bash
git add src/features/color src/shared src/core src/ui tests
git commit -m "refactor: remove legacy imports from color feature"
```

## 10. 阶段 5：Shared 边界修复

### 当前问题

`src/shared` 仍直接 import `src.features.*`，当前已知有：

```text
src/shared/ai_models/inference_engine.py -> src.features.composite.render.ai_cache_manager
src/shared/project/offline_asset_manager.py -> src.features.edit.core.project
src/shared/project/save_router.py -> src.features.edit.core.project
src/shared/project/topos_project_io.py -> src.features.edit.ui.relink_media_dialog
```

这违反 shared 的方向：shared 应被 feature 使用，而不是反向知道 feature 内部。

### 推荐修复方式

#### 5.1 为 Edit 项目能力定义 shared 协议

新增：

```text
src/shared/project/edit_project_protocol.py
```

只放协议和轻量 DTO：

```python
from typing import Protocol


class EditProjectLike(Protocol):
    ...
```

`shared/project` 只依赖协议，不 import `src.features.edit.core.project`。

#### 5.2 将 relink UI 从 shared 调用中移出

当前：

```text
src/shared/project/topos_project_io.py -> src.features.edit.ui.relink_media_dialog
```

推荐：

- shared/project 只抛出需要 relink 的数据结构。
- app 或 edit adapter 负责弹 UI。
- UI 仍归属 `src/features/edit/ui/relink_media_dialog.py`。

#### 5.3 AI cache manager 归属重新判断

当前：

```text
src/shared/ai_models/inference_engine.py -> src.features.composite.render.ai_cache_manager
```

两种可选方案：

- 如果 AI cache 是所有 AI 推理共享能力，迁到 `src/shared/ai_models/ai_cache_manager.py`。
- 如果它只服务 Composite，把 shared inference engine 中的直接依赖改成可注入 adapter。

不要让 shared 直接 import composite feature。

### 验证

```bash
rg -n "from src\.features\.|import src\.features\." src/shared -g "*.py"
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_topos_project_controller.py tests/edit/test_media_pool.py tests/test_composite_graph.py -q
```

验收标准：

- `src/shared` 不再 import `src.features.*`。
- Project save/load、offline relink、Composite AI 基础测试通过。

建议提交：

```bash
git add src/shared src/features/edit src/features/composite src/app tests
git commit -m "refactor: remove feature dependencies from shared layer"
```

## 11. 阶段 6：Media / Video / GPU 从 `src/core` 收口到 shared

### 当前问题

`src/core/media` 和 `src/core/video` 中仍有大量真实实现，不是 wrapper。

本次粗略扫描发现 `src/core` 中至少这些文件像真实实现：

```text
src/core/media/cache.py
src/core/media/decode_policy.py
src/core/media/decode_queue.py
src/core/media/disk_proxy_service.py
src/core/media/errors.py
src/core/media/frame_cache.py
src/core/media/frame_path.py
src/core/media/gpu_frame.py
src/core/media/metadata.py
src/core/media/performance_monitor.py
src/core/media/source.py
src/core/media/timecode.py
src/core/video/frame_provider_service.py
src/core/video/frame_queue.py
src/core/video/frame_request.py
src/core/video/yuv_gpu_self_test.py
```

### 目标归属

```text
src/core/media/*               -> src/shared/media/*
src/core/video/*               -> src/shared/video/*
src/core/video_decoder.py      -> src/shared/video/video_decoder.py
src/core/video_playback.py     -> src/shared/video/video_playback.py
src/core/video_async_renderer.py -> src/shared/video/video_async_renderer.py
src/core/video_texture_pool.py -> src/shared/video/video_texture_pool.py
src/core/video_upload.py       -> src/shared/video/video_upload.py
src/core/yuv_gpu_upload.py     -> src/shared/video/yuv_gpu_upload.py
src/core/gl_backend.py         -> src/shared/gpu/gl_backend.py
```

### 操作方式

1. 每次只迁一个子域：
   - media metadata/frame/source
   - media proxy/cache
   - video playback/session/frame provider
   - gpu/gl backend/upload

2. 使用 `git mv` 保留历史。

3. 旧路径保留 wrapper：

```python
"""Compatibility wrapper for shared media frame."""
from src.shared.media.frame import *
```

4. 迁移所有新代码 import。

5. 测试通过后再考虑删除 wrapper。

### 验证

每个子域迁移后都跑：

```bash
python3 -m compileall -q src
.venv/bin/python -m pytest tests/media/test_video_pipeline_smoke.py tests/media/test_color_frame_provider_adapter.py tests/test_video_upload_command.py -q
```

如果测试太慢，至少跑：

```bash
.venv/bin/python -m pytest tests/media/test_color_frame_provider_adapter.py tests/media/test_video_pipeline_smoke.py -q
```

验收标准：

- `src/shared/media` 和 `src/shared/video` 承载真实实现。
- `src/core/media` 和 `src/core/video` 只剩 wrapper。
- 非 video 功能不再直接依赖旧 `src.core.media` / `src.core.video`。

## 12. 阶段 7：Edit / Deliver 旧路径收口

### Edit

目标：

```text
src/core/edit/*                -> src/features/edit/core/*
src/ui/edit/*                  -> src/features/edit/ui/*
```

验证：

```bash
rg -n "from src\.core\.edit|from src\.ui\.edit|import src\.core\.edit|import src\.ui\.edit" src tests -g "*.py"
.venv/bin/python -m pytest tests/edit -q
```

验收标准：

- `src/features/edit` 不再依赖 `src.core.edit` / `src.ui.edit`。
- 旧 edit 路径可删除演练通过。

删除演练：

```bash
tmpdir=$(mktemp -d /tmp/topos-edit-legacy.XXXXXX)
mv src/core/edit "$tmpdir/core_edit"
mv src/ui/edit "$tmpdir/ui_edit"
.venv/bin/python -m pytest tests/edit -q
mv "$tmpdir/core_edit" src/core/edit
mv "$tmpdir/ui_edit" src/ui/edit
```

### Deliver

目标：

```text
src/core/export/*              -> src/shared/export/* 或 src/features/deliver/core/*
src/ui/export_panel.py         -> src/features/deliver/ui/export_panel.py 或 src/shared/export/export_panel.py
src/ui/export_dialog.py        -> src/features/deliver/ui/export_dialog.py
src/ui/quick_export_dialog.py  -> src/shared/export/quick_export_dialog.py
src/ui/deliver_page.py         -> src/features/deliver/ui/deliver_page.py
```

判断原则：

- 编码器、帧提供、序列导出这种通用能力放 `src/shared/export`。
- Deliver 页面、预设 UI、用户工作流放 `src/features/deliver/ui`。
- Timeline 到 export 的业务编排放 `src/features/deliver/core` 或 adapter。

验证：

```bash
rg -n "from src\.core\.export|from src\.ui\.export|from src\.ui\.deliver|import src\.core\.export|import src\.ui\.export" src tests -g "*.py"
.venv/bin/python -m pytest tests/edit/test_timeline_export.py tests/test_page_context.py -q
```

验收标准：

- Deliver 不依赖旧 export/core/ui 路径。
- Color/Edit/Composite 都能把输出交给 Deliver。

## 13. 阶段 8：`src/ui` 最终拆分与删除

### 当前问题

`src/ui` 中仍有 58 个 Python 文件，其中很多仍是实际实现，例如：

```text
src/ui/audio_mixer/audio_fader.py
src/ui/audio_mixer/audio_level_meter.py
src/ui/audio_mixer/audio_mixer_panel.py
src/ui/files_panel.py
src/ui/info_panel.py
src/ui/library_adapter.py
src/ui/library_panel.py
src/ui/playback_hud.py
src/ui/preferences_dialog.py
src/ui/resize_dialog.py
src/ui/script_panel.py
src/ui/shortcut_dialog.py
src/ui/video_control_panel.py
src/ui/welcome_dialog.py
src/ui/widgets/*
```

### 推荐归属

```text
src/ui/preferences_dialog.py       -> src/app/ui/preferences_dialog.py
src/ui/shortcut_dialog.py          -> src/app/ui/shortcut_dialog.py
src/ui/welcome_dialog.py           -> src/app/ui/welcome_dialog.py
src/ui/resize_dialog.py            -> src/features/color/ui/resize_dialog.py 或 app/ui
src/ui/files_panel.py              -> src/features/edit/ui/files_panel.py 或 app/ui/library
src/ui/library_panel.py            -> src/features/edit/ui/library_panel.py 或 app/ui/library
src/ui/info_panel.py               -> src/features/color/ui/info_panel.py 或 app/ui
src/ui/playback_hud.py             -> src/shared/ui/playback_hud.py
src/ui/video_control_panel.py      -> src/shared/ui/video_control_panel.py
src/ui/audio_mixer/*               -> src/features/edit/ui/audio_mixer/* 或 shared/ui/audio_mixer/*
src/ui/widgets/*                   -> src/shared/ui/widgets/*
src/ui/script_panel.py             -> src/features/scripting/ui/script_panel.py
```

### 操作

1. 对每个文件先判断是否属于 app、shared、某个 feature。
2. `git mv` 到目标目录。
3. 旧路径留下 wrapper。
4. 修改所有新代码 import。
5. 等所有测试都走新路径后，删除 `src/ui`。

### 验证

```bash
rg -n "from src\.ui|import src\.ui" src tests -g "*.py"
python3 -m compileall -q src
.venv/bin/python -m pytest tests/test_page_context.py tests/test_src_directory_boundaries.py -q
```

验收标准：

- 应用代码不再 import `src.ui.*`。
- `src/ui` 只剩 wrapper，随后可删除。

## 14. 阶段 9：`src/core` 最终删除演练

### 前置条件

必须先满足：

- `src/features/color` 无 `src.core` import。
- `src/features/edit` 无 `src.core.edit` import。
- `src/shared` 无旧 `src.core` 真实实现依赖。
- `src/app` 无 `src.core` import。
- 测试中的旧路径引用已迁移或明确删除。

### 删除演练

```bash
tmpdir=$(mktemp -d /tmp/topos-core-ui-final.XXXXXX)
mv src/core "$tmpdir/core"
mv src/ui "$tmpdir/ui"

python3 -m compileall -q src
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_modular_import_boundaries.py tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py tests/edit -q

mv "$tmpdir/core" src/core
mv "$tmpdir/ui" src/ui
```

如果删除演练失败：

1. 记录失败 import。
2. 回滚临时移动。
3. 迁移对应 import。
4. 重新演练。

### 最终删除

只有删除演练通过后才执行：

```bash
git rm -r src/core src/ui
git commit -m "refactor: remove legacy core and ui packages"
```

## 15. 阶段 10：把边界测试从 warning 升级为 hard assert

当前 `tests/test_src_directory_boundaries.py` 中这些问题还是 warning：

- Color legacy imports。
- Shared imports features。
- Top-level legacy dirs。

当对应迁移完成后，把 warning 改成 assert。

### 验收测试应包括

```python
def test_features_color_does_not_import_legacy():
    assert offenders == []

def test_shared_does_not_import_features():
    assert offenders == []

def test_src_top_level_only_app_shared_features():
    assert found_legacy == []
```

同时增加对顶层文件的严格检查：

允许：

```text
app
shared
features
__init__.py
```

是否允许 `main.py` 取决于最终入口策略。如果目标是极简 `src/`，则不允许 `src/main.py`。

验证：

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

验收标准：无 warning、无失败。

## 16. 阶段 11：Composite 修复（只在 src 瘦身完成后开始）

### 执行时机

只有当以下条件满足后，才开始 Composite 功能修复：

- `src/core` 已删除或删除演练通过。
- `src/ui` 已删除或删除演练通过。
- `src/composite` 已不存在。
- `src/features/composite` 不依赖旧路径。
- Color/Edit/Deliver 基础测试通过。

### Composite 修复范围

按顺序修，不要混在一起：

1. Composite 页面打开与基本交互。
2. 节点新增、删除、连接、选择。
3. 属性面板同步。
4. Viewer 渲染、缩放、overlay、handles。
5. Render scheduler / cache / tile。
6. 保存、加载、项目集成。
7. Deliver export 集成。
8. Composite 禁用和删除演练。

### 基础验证

```bash
.venv/bin/python -m pytest tests/test_nodes tests/test_composite_graph.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
```

如果有 GUI 环境，手动验收：

1. 启动应用。
2. 进入 Composite 页面。
3. 新建 graph。
4. 添加 Read/Input 节点。
5. 添加 Transform / Merge / Color 节点。
6. 连线。
7. Viewer 显示结果。
8. 保存项目。
9. 关闭并重新打开项目。
10. 从 Edit clip 跳转到 Composite。
11. 从 Deliver 导出 Composite output。

验收标准：

- Composite 页面可打开。
- Composite 不影响 Color 页面。
- 禁用 Composite 后 Color/Edit/Deliver 仍可用。
- 删除 `src/features/composite` 的演练在禁用 flag 下通过。

## 17. 最终验收清单

执行完成后，必须全部满足：

- `find src -maxdepth 1 -mindepth 1 -print | sort` 只显示目标项。
- `src/core` 不存在。
- `src/ui` 不存在。
- `src/composite` 不存在。
- `src/utils` 不存在。
- `src/config.json` 不存在。
- `src/metadata.db`、`src/topos.db` 不存在。
- `src/.DS_Store`、`src/.images_cache`、`src/__pycache__` 不存在。
- `src/app` 不 deep import feature internals。
- `src/features/color` 不 import `src.core` / `src.ui` / `src.utils`。
- `src/features/edit` 不 import `src.core.edit` / `src.ui.edit`。
- `src/features/composite` 不 import `src.composite`。
- `src/features/deliver` 不 import 旧 export UI/core 路径。
- `src/shared` 不 import `src.features`。
- `tests/test_src_directory_boundaries.py` 无 warning、无失败。
- `tests/test_modular_import_boundaries.py` 通过。
- Color 基础调色手动验收通过。
- Edit 基础 timeline 测试通过。
- Composite 基础 graph/node 测试通过。
- Deliver 基础 export 测试通过。

最终命令建议：

```bash
python3 -m compileall -q src
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py tests/test_modular_import_boundaries.py -q
.venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py tests/test_composite_graph.py -q
.venv/bin/python -m pytest tests/edit -q
```

如果机器环境支持 GPU/GUI，再额外跑手动验收。

## 18. 给下一轮 AI 的提示词

```text
你现在接手 Topos Color 的 src 目录最终瘦身。

请严格阅读并执行：
docs/src_directory_remaining_cleanup_plan_2026-05-26.md

当前目标不是修 Composite 功能，而是先完成 src 顶层瘦身：
src/ 最终只保留 app/shared/features（以及必要的 __init__.py）。

优先级：
1. 先修当前红灯测试：
   - tests/test_src_directory_boundaries.py
   - tests/test_page_context.py
2. 再清理 src 顶层运行产物和配置文件。
3. 再把 app/main_window.py 瘦成真正应用壳。
4. 再收口 Color 旧路径 import，注意只改 import/归属，不改调色算法。
5. 再修 shared -> features 的反向依赖。
6. 再迁移 media/video/gpu/edit/deliver/ui。
7. 删除演练通过后，删除 src/core 和 src/ui。
8. 最后才开始 Composite 功能修复。

硬约束：
- 不要顺手重写 Color 核心调色算法。
- 不要把 Composite 功能修复混进目录瘦身提交。
- 每个阶段都要运行计划中写明的验证命令。
- 每个阶段单独提交。
- 如果发现旧路径 wrapper 仍被测试依赖，先迁测试 import，再删除 wrapper。
```

