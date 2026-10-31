# Topos Color 功能模块化实施大纲

版本：2026-05-24  
适用项目：Topos Color  
目标读者：后续负责执行重构的 AI Agent / 开发者  
核心要求：**调色主线稳定优先，先外围解耦，后目录迁移**

---

## 0. 这份文档的目的

Topos Color 当前同时包含：

- Edit：剪辑、时间线、媒体池、源监看、节目监看
- Color：调色、图层、遮罩、LUT、插件、GPU 渲染管线
- Composite：节点合成、节点图、合成 Viewer、节点导出
- Deliver：导出页面和导出流程

当前代码已经有一些模块化雏形：

- `src/core/edit/` 和 `src/ui/edit/` 基本属于剪辑模块
- `src/composite/` 基本属于合成模块
- 调色核心分散在 `src/core/`、`src/ui/` 和 `src/ui/widgets/`
- `src/ui/main_window.py` 是当前最大的耦合中心

本计划的最终目标是让剪辑、调色、合成、导出逐步变成可注册功能模块：

1. 可以通过配置禁用某个功能。
2. 禁用某功能后，其页面、菜单、保存路由、导出入口不会出现。
3. 更进一步，删除某功能目录后，其它功能仍可启动和使用。
4. 调色核心管线作为当前软件核心，前期只包接口、不乱动内部算法。

---

## 1. 总体架构目标

长期目标目录结构：

```text
src/
  app/
    __init__.py
    app_context.py
    feature_registry.py
    page_contract.py
    save_contract.py
    module_loader.py

  shared/
    __init__.py
    keyframe.py
    media_contracts.py
    project_contracts.py
    render_contracts.py
    feature_flags.py

  features/
    __init__.py
    color/
      __init__.py
      module.py
      contracts.py
      adapters.py
      ui/
      core/
    edit/
      __init__.py
      module.py
      contracts.py
      adapters.py
      ui/
      core/
    composite/
      __init__.py
      module.py
      contracts.py
      adapters.py
      ui/
      core/
      nodes/
    deliver/
      __init__.py
      module.py
      contracts.py
      adapters.py
      ui/
      core/
```

注意：这是长期目标，不是第一阶段就直接搬目录。

第一阶段应保持现有路径，先建立可禁用机制和依赖边界。  
第二阶段再抽公共代码。  
第三阶段才开始把页面和功能注册逻辑移出 `main_window.py`。  
调色核心文件最后才考虑迁移。

---

## 2. 调色主线冻结区

以下文件是 Color 功能的核心稳定区。前几个阶段原则上不要重构内部算法，不要改渲染逻辑，不要改 GLSL 拼接顺序，不要改资源生命周期。

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
src/ui/canvas.py
src/ui/widgets/color_wheel.py
src/ui/adjustment_panel.py
src/ui/hsl_curves_panel.py
src/ui/lut_panel.py
src/ui/mask_panel.py
src/ui/qualifier_panel.py
src/ui/soft_clip_panel.py
src/ui/raw_panel.py
src/ui/plugin_panel.py
```

### 2.1 禁止事项

在完成 Feature Registry 之前，不要做下面这些事：

- 不要把 `ImageEngine` 直接移动到新目录。
- 不要拆分 `ShaderPipeline` 的 GLSL 生成顺序。
- 不要把 `AsyncRenderer` 的线程模型改成同步调用。
- 不要让 UI 线程直接调用 ModernGL。
- 不要改变 `Layer` 的序列化字段含义。
- 不要重命名已有项目文件中的调色字段。
- 不要把 Color 变成可选功能。Color 是当前主干，先永远启用。

### 2.2 允许事项

前期允许做：

- 在调色核心外面增加接口包装。
- 在 `MainWindow` 旁边新增 `ColorFacade` 或 `ColorFeatureAdapter`。
- 给其它模块提供只读调色输出接口。
- 用兼容层替代直接访问 `self.engine` 的跨模块调用。
- 增加测试和文档。

---

## 3. 当前已知耦合点

执行前必须先理解这些耦合点。

### 3.1 启动入口硬依赖 Composite

文件：

```text
src/main.py
```

当前启动时有类似逻辑：

```python
from src.composite.node_engine import discover_and_register_plugins
```

问题：如果删除 `src/composite/`，应用启动会失败。

目标：改为按 feature flag 条件导入，并捕获 `ImportError`。

### 3.2 MainWindow 顶部 import 太多功能

文件：

```text
src/ui/main_window.py
```

当前顶部直接 import：

- 调色 UI 面板
- Edit 页面和 Edit 面板
- Composite 页面
- Deliver 页面
- Edit Project 数据模型
- 视频播放和解码类
- SaveRouter

问题：`MainWindow` 成为所有功能的编译时依赖中心。删除任意功能文件夹都可能导致启动失败。

目标：`MainWindow` 逐步变成 App Shell，只管理窗口、Dock、页面容器和通用菜单，不直接知道所有功能内部类。

### 3.3 Edit 依赖 Composite 的 keyframe

文件：

```text
src/core/edit/effect_instance.py
src/core/edit/timeline_v2.py
src/core/edit/commands.py
```

当前 Edit 使用：

```python
from src.composite.keyframe import Keyframe, KeyframeTrack, InterpolationType
```

问题：Keyframe 是共享概念，不应该属于 Composite。删除 Composite 时 Edit 会坏。

目标：迁移到：

```text
src/shared/keyframe.py
```

旧路径保留兼容 re-export。

### 3.4 SaveRouter 硬编码页面类型

文件：

```text
src/core/save_router.py
```

当前逻辑按页面名写死：

```python
if page_lower == "library":
elif page_lower == "color":
elif page_lower == "edit":
elif page_lower == "composite":
elif page_lower == "deliver":
```

问题：模块禁用后仍有保存分支；后续新增/删除功能不方便。

目标：SaveRouter 保留统一入口，但内部改为 handler 注册。

### 3.5 Composite 节点注册硬编码

文件：

```text
src/composite/node_engine.py
```

当前 `NODE_REGISTRY` 和 `NODE_CATEGORIES` 通过大量直接 import 建立。

问题：单个节点缺失可能导致整个合成模块 import 失败。后续节点包不易拆分。

目标：后期改成节点包注册机制。第一阶段先不动，避免扩大风险。

---

## 4. 分阶段实施总览

推荐执行顺序：

1. 阶段 0：建立基线和安全网，不改功能。
2. 阶段 1：加 feature flag，实现功能禁用，不搬目录。
3. 阶段 2：抽 `shared.keyframe`，解除 Edit 对 Composite 的硬依赖。
4. 阶段 3：建立 `app` 层和 Feature Registry。
5. 阶段 4：Composite 接入 Registry，做到可禁用。
6. 阶段 5：SaveRouter handler 化。
7. 阶段 6：Color 增加外包装接口，但不移动核心管线。
8. 阶段 7：Composite 做可删除验证。
9. 阶段 8：Edit 接入 Registry，做到可禁用。
10. 阶段 9：Deliver 接入 Registry。
11. 阶段 10：最后评估是否移动调色核心文件。

每个阶段必须做到：

- 小步提交。
- 每阶段有明确验证命令。
- 每阶段有人工验证清单。
- 任一验证失败，先修复，不进入下一阶段。

---

## 5. 阶段 0：建立基线和安全网

### 5.1 目标

不改功能，只记录当前状态，建立后续重构的验收标准。

### 5.2 具体任务

1. 新增依赖扫描命令记录。
2. 记录当前启动路径。
3. 记录当前页面列表。
4. 记录当前调色主链。
5. 运行现有测试，保存结果。

### 5.3 建议新增文档

可以新增：

```text
docs/modularization_baseline_YYYY-MM-DD.md
```

内容包括：

- 当前 git commit hash
- 当前 Python 版本
- 当前 Qt/PyQt 版本
- 当前测试结果
- 当前手工验证结果
- 当前不能动的核心文件列表

### 5.4 推荐命令

```bash
git status --short
python -m compileall src
pytest
rg -n "from src.composite|import src.composite" src tests
rg -n "from src.core.edit|import src.core.edit" src tests
rg -n "from src.ui.edit|import src.ui.edit" src tests
rg -n "from src.core.image_engine|from src.core.shader_compiler|from src.core.async_renderer" src tests
```

如果 `pytest` 全量太慢，至少运行：

```bash
pytest tests/test_nodes
pytest tests/media
python -m compileall src
```

### 5.5 人工验证清单

必须逐项验证：

- 应用可启动。
- Library 页可打开文件。
- Color 页可打开图片。
- Color 页可调整 Lift/Gamma/Gain。
- Color 页可调整曲线。
- Color 页可启用 LUT。
- Color 页可使用 Mask。
- Color 页可使用 Qualifier。
- Color 页可使用 Soft Clip。
- Color 页 Canvas 刷新正常。
- Color 页保存后重新打开参数不丢。
- Edit 页可创建项目。
- Edit 页可导入媒体。
- Edit 页时间线可播放。
- Composite 页可打开。
- Composite 页可创建节点。
- Deliver 页可进入。

### 5.6 通过标准

- `python -m compileall src` 通过。
- 关键 pytest 通过，或已有失败被记录为基线失败。
- 人工验证无新增问题。

---

## 6. 阶段 1：增加 Feature Flag，不搬文件

### 6.1 目标

实现“可禁用功能”的最小版本。先不移动目录。

### 6.2 建议新增文件

```text
src/shared/__init__.py
src/shared/feature_flags.py
```

### 6.3 Feature Flag 数据结构

建议默认配置：

```python
DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "library": True,
    "color": True,
    "edit": True,
    "composite": True,
    "deliver": True,
}
```

建议读取顺序：

1. 默认值。
2. `config.json` 中的 `features` 字段。
3. 环境变量覆盖，例如：
   - `TOPOS_FEATURE_EDIT=0`
   - `TOPOS_FEATURE_COMPOSITE=0`
   - `TOPOS_FEATURE_DELIVER=0`

注意：`color` 暂时不允许被关闭。即使配置里写 `color=false`，也应该强制 True，并记录 warning。

### 6.4 建议接口

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class FeatureFlags:
    library: bool = True
    color: bool = True
    edit: bool = True
    composite: bool = True
    deliver: bool = True

    def is_enabled(self, feature_id: str) -> bool:
        ...
```

### 6.5 修改点

文件：

```text
src/main.py
src/ui/main_window.py
src/utils/config_manager.py
```

任务：

1. 在启动早期读取 feature flags。
2. `src/main.py` 中 Composite 插件发现改为：

```python
if feature_flags.is_enabled("composite"):
    try:
        from src.composite.node_engine import discover_and_register_plugins
    except ImportError:
        logger.warning("Composite module unavailable; skipping node plugin discovery")
    else:
        ...
```

3. `MainWindow` 初始化页面时，根据 feature flags 决定页面列表。
4. 如果 `composite=false`，不创建 Composite 页，不显示 Composite 菜单，不注册 Composite Dock。
5. 如果 `edit=false`，暂时可以先隐藏 Edit 页，但如果代码耦合较多，先只支持 Composite flag，Edit 延后。

### 6.6 验证命令

```bash
python -m compileall src
pytest tests/test_config_manager.py
pytest tests/test_nodes
```

使用环境变量验证：

```bash
TOPOS_FEATURE_COMPOSITE=0 python src/main.py
```

### 6.7 人工验证清单

Composite 开启：

- Composite 页存在。
- Composite 节点图可打开。
- Color 页面正常。

Composite 关闭：

- 应用可启动。
- Composite 页不出现在页面切换中。
- Composite Dock 不出现。
- 面板菜单中没有 Composite 专属项。
- Edit 右键 Open in Composite 不崩溃，应禁用或提示“Composite module is disabled”。
- Color 调色功能全部正常。

### 6.8 通过标准

- Composite 可通过配置隐藏。
- 关闭 Composite 不影响 Color。
- 没有因为 Composite 关闭导致启动失败。

---

## 7. 阶段 2：抽出 shared.keyframe

### 7.1 目标

解除 Edit 对 Composite 的基础模型依赖。

### 7.2 新增文件

```text
src/shared/keyframe.py
```

### 7.3 迁移策略

不要直接删除 `src/composite/keyframe.py`。  
使用兼容 re-export：

```python
# src/composite/keyframe.py
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType

__all__ = ["Keyframe", "KeyframeTrack", "InterpolationType"]
```

然后修改 Edit 引用：

```python
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType
```

### 7.4 需要检查的文件

```bash
rg -n "src.composite.keyframe|from \\.\\.composite.keyframe|from composite.keyframe" src tests
```

已知重点：

```text
src/core/edit/effect_instance.py
src/core/edit/timeline_v2.py
src/core/edit/commands.py
```

### 7.5 验证命令

```bash
python -m compileall src
pytest tests/media
pytest tests/test_nodes
rg -n "src.composite.keyframe" src/core/edit src/ui/edit
```

最后一条命令应没有结果。

### 7.6 人工验证清单

- 打开 Edit 项目。
- 带关键帧的 clip 参数能加载。
- 调整关键帧后保存。
- 重新打开项目，关键帧不丢。
- Composite 节点关键帧仍正常。
- Color 页调色无变化。

### 7.7 通过标准

- Edit 不再直接 import `src.composite.keyframe`。
- 旧 Composite 代码仍能通过旧路径使用 keyframe。
- 所有关键帧序列化格式不变。

---

## 8. 阶段 3：建立 app 层和 Feature Registry

### 8.1 目标

让应用壳通过模块注册页面，而不是 `MainWindow` 顶部直接 import 所有功能。

### 8.2 新增文件

```text
src/app/__init__.py
src/app/app_context.py
src/app/page_contract.py
src/app/feature_registry.py
src/app/module_loader.py
```

### 8.3 AppContext 建议内容

```python
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class AppContext:
    main_window: Any
    config_manager: Any
    feature_flags: Any
    i18n: Any
    logger: Any
```

注意：第一版可以用 `Any` 降低迁移成本，但新函数仍要有类型注解。

### 8.4 PageDescriptor 建议内容

```python
from dataclasses import dataclass
from typing import Callable, Any

@dataclass(frozen=True)
class PageDescriptor:
    page_id: str
    title: str
    factory: Callable[[Any], Any]
    default_enabled: bool = True
```

### 8.5 FeatureModule 协议

```python
from typing import Protocol

class FeatureModule(Protocol):
    feature_id: str

    def is_available(self) -> bool:
        ...

    def register_pages(self, context: AppContext) -> list[PageDescriptor]:
        ...

    def register_actions(self, context: AppContext) -> None:
        ...

    def register_save_handlers(self, context: AppContext) -> None:
        ...
```

### 8.6 Registry 责任

`FeatureRegistry` 应负责：

- 保存所有模块。
- 判断模块是否启用。
- 判断模块是否可 import。
- 注册页面。
- 查询模块能力。
- 提供 `is_feature_available(feature_id)`。

不负责：

- 不直接做 UI 布局。
- 不直接保存文件。
- 不直接执行渲染。

### 8.7 第一版迁移范围

只接入 Composite。  
不要同时迁移 Color/Edit/Deliver。

原因：Composite 已经有独立目录，最适合作为样板。

### 8.8 验证命令

```bash
python -m compileall src
pytest tests/test_nodes
```

检查 `main_window.py` 中 Composite import 是否减少：

```bash
rg -n "from src.composite|import src.composite" src/ui/main_window.py
```

### 8.9 人工验证清单

- Composite 开启时，页面仍正常。
- Composite 关闭时，页面不出现。
- Color 页面启动速度和功能无明显变化。
- 旧布局中如果保存过 Composite dock，关闭 Composite 时不会显示空 dock。

### 8.10 通过标准

- Composite 页面创建由 Registry 驱动。
- `MainWindow` 不再需要在文件顶部 import Composite 页面类。
- Color 功能无变化。

---

## 9. 阶段 4：Composite 接入 Registry

### 9.1 目标

把 Composite 从 “MainWindow 内部直接创建” 改成 “Composite 模块自己注册”。

### 9.2 新增文件

长期目标路径：

```text
src/features/composite/__init__.py
src/features/composite/module.py
src/features/composite/adapters.py
```

第一阶段为了少搬目录，也可以先放：

```text
src/composite/module.py
src/composite/adapters.py
```

推荐先用 `src/composite/module.py`，等稳定后再整体迁移到 `src/features/composite/`。

### 9.3 CompositeModule 职责

`CompositeModule` 应负责：

- 判断 `src.composite.ui.composite_page` 是否可 import。
- 创建 CompositePage。
- 创建 CompositeViewer。
- 创建 NodePropertiesPanel。
- 注册 Composite dock。
- 注册 Composite 相关菜单。
- 注册 SaveRouter handler。
- 提供 `open_composite(composite_id)` 能力。

### 9.4 MainWindow 保留职责

`MainWindow` 保留：

- 窗口生命周期。
- 页面切换。
- Dock 布局。
- 状态栏。
- 顶层菜单容器。
- 通用快捷键。

### 9.5 迁移建议

不要一次性把所有 Composite 逻辑从 `main_window.py` 删除。  
建议分三步：

1. 先把 import 改成 lazy import。
2. 再把创建 CompositePage/CompositeViewer 移到 `CompositeModule`。
3. 最后把 Composite 事件处理和保存逻辑移动。

### 9.6 风险点

这些逻辑当前可能和 `MainWindow` 深度耦合：

- `_on_composite_frame_ready`
- `_on_composite_metadata_ready`
- `_on_composite_status_message`
- `_handle_enter_composite_with_context`
- `_on_open_in_composite_requested`
- `_switch_to_composite_page`
- `_get_composite_graph_data`
- `_get_composite_state_data`
- `_export_composite`
- `_render_composite_full_res`
- `_on_color_output_rendered`
- undo/redo 中 Composite 优先处理

迁移时每次只移动一个小组。

### 9.7 验证命令

```bash
python -m compileall src
pytest tests/test_nodes
pytest tests/media
```

### 9.8 人工验证清单

Composite 开启：

- 进入 Composite 页面。
- 创建 `Image/Input -> Grade -> Output` 类似图。
- Viewer 显示正常。
- 参数面板正常。
- undo/redo 正常。
- 从 Edit 打开 Composite 正常。
- 保存工程后重新打开，Composite 图恢复。

Composite 关闭：

- 应用启动。
- Composite 页面隐藏。
- Edit 中打开合成入口禁用或提示。
- 保存工程不崩溃。
- Color 页面正常。

### 9.9 通过标准

- Composite 由模块注册。
- 关闭 Composite 不影响启动。
- 关闭 Composite 不影响 Color。

---

## 10. 阶段 5：SaveRouter Handler 化

### 10.1 目标

保存逻辑从硬编码页面分支改成模块注册 handler。

### 10.2 新增概念

```python
class SaveHandler(Protocol):
    context_id: str

    def can_save(self, request: SaveRequest) -> bool:
        ...

    def save(self, request: SaveRequest) -> bool:
        ...
```

### 10.3 建议新增文件

```text
src/app/save_contract.py
```

或先放在：

```text
src/core/save_router.py
```

为了减少改动，第一版可以直接在 `SaveRouter` 中加入：

```python
self._handlers: dict[str, Callable[[], bool]] = {}

def register_handler(self, page_id: str, handler: Callable[[], bool]) -> None:
    self._handlers[page_id.lower()] = handler
```

### 10.4 迁移顺序

1. 保留现有 `_save_from_color`、`_save_from_edit`、`_save_from_composite`。
2. 增加 handler 字典。
3. 初始化时注册现有方法：

```python
router.register_handler("color", router._save_from_color)
router.register_handler("edit", router._save_from_edit)
router.register_handler("composite", router._save_from_composite)
router.register_handler("deliver", router._save_from_deliver)
```

4. `save()` 优先查 handler。
5. Feature 关闭时，不注册对应 handler。
6. 第二轮再把每个 handler 移到对应模块。

### 10.5 验证命令

```bash
python -m compileall src
pytest
```

### 10.6 人工验证清单

- Color 单图保存。
- Color `.topos` 保存。
- Edit 时间线保存。
- Composite 图保存。
- Deliver 设置保存或导出不受影响。
- Composite 关闭时执行保存，不崩溃。
- 打开含 Composite 数据工程，关闭 Composite 时保存，不应清空未知合成数据。

### 10.7 通过标准

- SaveRouter 仍是统一保存入口。
- 关闭某个模块时不会调用它的保存 handler。
- Color 保存行为不变。

---

## 11. 阶段 6：Color 外包装接口，不移动核心

### 11.1 目标

让其它模块通过 Color 接口访问调色输出，不直接访问 `MainWindow.engine`、`ImageEngine` 内部状态。

### 11.2 建议新增文件

```text
src/features/color/__init__.py
src/features/color/contracts.py
src/features/color/adapters.py
src/features/color/module.py
```

如果暂时不想新建 `features/`，可先放：

```text
src/core/color_facade.py
```

但长期仍建议放到 `features/color/`。

### 11.3 ColorFacade 建议接口

```python
class ColorFacade:
    def get_current_texture_id(self) -> int | None:
        ...

    def get_current_output_size(self) -> tuple[int, int] | None:
        ...

    def get_current_revision(self) -> int:
        ...

    def request_render(self, reason: str) -> None:
        ...

    def get_current_color_stack_id(self) -> str | None:
        ...

    def load_color_stack(self, stack_id: str) -> bool:
        ...

    def save_current_color_stack(self) -> bool:
        ...
```

注意：第一版可以只实现 Composite/Edit 实际需要的接口，不要过度设计。

### 11.4 修改方向

当前一些逻辑可能直接访问：

```python
self.engine
self.canvas
self.current_file
self.current_page
self.composite_page.update_color_output(...)
```

目标是将跨模块访问改成：

```python
self.color_facade.get_current_texture_id()
self.color_facade.request_render("composite_input_dirty")
```

### 11.5 禁止修改

本阶段不要改：

- `ShaderPipeline` 拼接顺序。
- `AsyncRenderer.submit()` 协议。
- ModernGL 资源释放逻辑。
- Layer 字段含义。
- 调色面板参数名。

### 11.6 验证命令

```bash
python -m compileall src
pytest tests/test_nodes
pytest tests/media
```

检查跨模块直接访问是否减少：

```bash
rg -n "self.engine" src/ui/main_window.py
rg -n "update_color_output|get_current_texture|current_texture" src/ui/main_window.py src/composite src/features
```

### 11.7 调色人工回归清单

这是本计划最重要的验证清单。

必须验证：

- 打开普通 PNG/JPEG。
- 打开高位深 TIFF/EXR，如果当前环境支持。
- 调整 Lift/Gamma/Gain。
- 调整 Offset/Contrast/Saturation。
- 调整 Curves。
- 调整 HSL Curves。
- 应用 LUT。
- 应用 Raw 参数。
- 启用 Mask。
- 调整 Mask 羽化。
- 启用 Qualifier。
- 调整 Soft Clip。
- 启用插件。
- 多图层混合。
- undo/redo 调色参数。
- 保存工程。
- 重新打开工程。
- 导出结果。

### 11.8 视觉一致性建议

如果有时间，准备一个固定测试素材和固定调色参数，导出重构前后图片：

```text
tests/visual/fixtures/color_regression_input.exr
tests/visual/outputs/before_ref.exr
tests/visual/outputs/after_ref.exr
```

用脚本比较：

- 最大像素差异
- 平均像素差异
- alpha 是否一致
- 尺寸是否一致
- 色彩空间标签是否一致

### 11.9 通过标准

- 调色结果视觉一致。
- 调色交互实时性无明显退化。
- Composite/Edit 通过接口拿 Color 输出。
- 没有扩大调色核心修改范围。

---

## 12. 阶段 7：Composite 可删除验证

### 12.1 目标

让 `src/composite/` 被移走时，Color 和 Edit 仍可启动和使用。

### 12.2 前置条件

必须先完成：

- Feature flag。
- Composite lazy import。
- shared.keyframe。
- Composite Module。
- SaveRouter handler 化。

### 12.3 删除测试命令

不要真的删除，使用 rename：

```bash
mv src/composite src/composite.disabled
python -m compileall src
TOPOS_FEATURE_COMPOSITE=0 python src/main.py
mv src/composite.disabled src/composite
```

如果启动需要 GUI 环境，则至少运行：

```bash
mv src/composite src/composite.disabled
python -m compileall src
python - <<'PY'
from src.shared.feature_flags import FeatureFlags
print(FeatureFlags(composite=False).is_enabled("composite"))
PY
mv src/composite.disabled src/composite
```

### 12.4 常见失败原因

如果删除测试失败，优先检查：

```bash
rg -n "from src.composite|import src.composite" src tests
```

允许存在的情况：

- Composite 模块内部引用 Composite。
- 测试 Composite 的文件引用 Composite。
- try/except ImportError 内的可选引用。

不允许存在的情况：

- `src/main.py` 顶部或启动必经路径硬 import Composite。
- `src/ui/main_window.py` 顶部硬 import Composite。
- `src/core/edit/` 硬 import Composite。
- `src/core/save_router.py` 在 import 时硬 import Composite。

### 12.5 人工验证清单

Composite 文件夹临时移走并关闭 feature flag 后：

- 应用可启动。
- Library 可用。
- Color 可用。
- Edit 可用。
- Save 可用。
- 打开含 Composite 数据工程不崩溃。
- UI 提示 Composite 不可用，而不是显示空页面。

### 12.6 通过标准

- `src/composite/` 不存在时，Color/Edit 不崩溃。
- Composite 相关工程数据不会被误删。
- 恢复 `src/composite/` 后 Composite 功能恢复。

---

## 13. 阶段 8：Edit 模块化

### 13.1 目标

让 Edit 也可以被 feature flag 禁用。  
短期不要求删除 `src/core/edit/` 和 `src/ui/edit/`，先做到关闭页面。

### 13.2 建议新增文件

```text
src/features/edit/__init__.py
src/features/edit/module.py
src/features/edit/contracts.py
src/features/edit/adapters.py
```

### 13.3 EditModule 职责

EditModule 负责：

- 创建 EditPage。
- 创建 MediaPoolPanel。
- 创建 TimelinePanel。
- 创建 EditPropertiesPanel。
- 创建 EditEffectsPanel。
- 注册 Edit 保存 handler。
- 注册 Edit 菜单和快捷键。
- 提供当前 timeline / selected clip / selected asset 给其它模块。

### 13.4 Color 与 Edit 的边界

Color 不应该知道：

- TimelineSequence 内部结构。
- Track 内部结构。
- ClipInstance 的全部字段。
- EditPage UI 实例。

Color 只应该接收：

```python
ColorOpenRequest(
    asset_id=...,
    clip_id=...,
    frame=...,
    color_stack_id=...,
)
```

### 13.5 Edit 与 Color 的边界

Edit 不应该直接操作：

- `ImageEngine.layers`
- `ShaderPipeline`
- `AsyncRenderer`
- Color 面板内部控件

Edit 应该通过：

```python
color_feature.open_color_for_clip(request)
```

### 13.6 验证命令

```bash
python -m compileall src
pytest tests/media
```

关闭 Edit：

```bash
TOPOS_FEATURE_EDIT=0 python src/main.py
```

### 13.7 人工验证清单

Edit 开启：

- 创建项目。
- 导入视频。
- 拖到时间线。
- 播放时间线。
- 选择 clip。
- 从 clip 打开 Color。
- 保存项目。

Edit 关闭：

- 应用可启动。
- Edit 页隐藏。
- Color 单图/单视频调色可用。
- 打开需要 Edit 的工程时提示 Edit 不可用。
- 不出现空 timeline dock。

### 13.8 通过标准

- Edit 可禁用。
- Color 单素材工作流不依赖 Edit。
- 禁用 Edit 不影响 Color 保存。

---

## 14. 阶段 9：Deliver 模块化

### 14.1 目标

Deliver 页面作为导出功能模块注册。  
导出源可以来自 Color/Edit/Composite，但 Deliver 本身不应该硬依赖这些模块内部实现。

### 14.2 建议接口

定义导出源协议：

```python
class ExportSource(Protocol):
    source_id: str
    display_name: str

    def is_available(self) -> bool:
        ...

    def render_frame(self, frame: int) -> Any:
        ...

    def get_duration(self) -> int | None:
        ...

    def get_format(self) -> Any:
        ...
```

Color、Edit、Composite 分别注册自己的 export source。

### 14.3 修改方向

当前 Deliver 如果直接调用 MainWindow 的：

- `_export_color`
- `_export_composite`
- `_render_composite_full_res`
- timeline export

后续应改为：

```python
export_registry.get_source("color")
export_registry.get_source("composite")
export_registry.get_source("timeline")
```

### 14.4 验证命令

```bash
python -m compileall src
pytest
```

### 14.5 人工验证清单

- Color 导出。
- Composite 开启时 Composite 导出。
- Composite 关闭时 Composite 导出选项隐藏。
- Edit 开启时时间线导出。
- Edit 关闭时时间线导出选项隐藏。
- 导出预设正常。

### 14.6 通过标准

- Deliver 可禁用。
- Deliver 不强迫导入 Composite/Edit。
- 导出源由功能模块注册。

---

## 15. 阶段 10：目录迁移

### 15.1 目标

在功能注册和边界稳定后，再移动文件。

### 15.2 推荐迁移顺序

1. 迁移 Composite。
2. 迁移 Edit。
3. 迁移 Deliver。
4. 最后评估 Color。

### 15.3 Composite 迁移

从：

```text
src/composite/
```

到：

```text
src/features/composite/
```

需要保留兼容层：

```text
src/composite/__init__.py
```

短期 re-export：

```python
from src.features.composite import *
```

或者保留旧路径一段时间，逐步改 import。

### 15.4 Edit 迁移

从：

```text
src/core/edit/
src/ui/edit/
```

到：

```text
src/features/edit/core/
src/features/edit/ui/
```

迁移风险较高，必须在 Edit 可禁用之后进行。

### 15.5 Color 迁移

短期不推荐移动：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
```

如果未来必须移动，建议先只建立 facade：

```text
src/features/color/module.py
src/features/color/contracts.py
```

核心仍留在 `src/core/`。

### 15.6 验证命令

每移动一批文件后运行：

```bash
python -m compileall src
pytest
rg -n "from src.composite|import src.composite" src
rg -n "from src.core.edit|import src.core.edit" src
rg -n "from src.ui.edit|import src.ui.edit" src
```

### 15.7 通过标准

- 旧工程可打开。
- 旧 import 兼容层有效。
- 新代码只使用新路径。
- 调色主链无视觉变化。

---

## 16. AI 执行者工作协议

后续 AI 执行每个阶段时，必须遵守以下协议。

### 16.1 每次开始前

运行：

```bash
git status --short
python -m compileall src
```

阅读：

```text
.clinerules
AGENTS.md
docs/feature_modularization_implementation_plan_2026-05-24.md
```

如果工作区已有修改，不要 revert，不要覆盖。  
只改当前阶段需要的文件。

### 16.2 每次改动范围

每次只做一个小目标，例如：

- 只加 feature flags。
- 只抽 shared.keyframe。
- 只让 Composite lazy import。
- 只给 SaveRouter 增加 handler 注册。

不要同时做：

- 搬目录
- 改保存格式
- 改调色渲染
- 改 UI 布局
- 改导出

### 16.3 每次结束前

必须输出：

- 改了哪些文件。
- 为什么改。
- 如何验证。
- 哪些测试通过。
- 哪些测试没跑，为什么。
- 是否触碰调色核心冻结区。

### 16.4 遇到调色核心文件时

如果必须修改以下文件：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
```

必须先做：

1. 说明为什么不能绕开。
2. 列出影响范围。
3. 只做最小修改。
4. 运行调色人工回归清单。
5. 提醒需要 Review。

---

## 17. 推荐测试矩阵

### 17.1 Feature flag 矩阵

至少测试：

```text
color=true, edit=true,  composite=true,  deliver=true
color=true, edit=true,  composite=false, deliver=true
color=true, edit=false, composite=true,  deliver=true
color=true, edit=false, composite=false, deliver=true
color=true, edit=true,  composite=false, deliver=false
```

注意：`color=false` 暂不支持。

### 17.2 文件夹删除矩阵

只在对应模块完成可删除目标后测试：

```bash
mv src/composite src/composite.disabled
TOPOS_FEATURE_COMPOSITE=0 python -m compileall src
mv src/composite.disabled src/composite
```

后续 Edit 完成后：

```bash
mv src/features/edit src/features/edit.disabled
TOPOS_FEATURE_EDIT=0 python -m compileall src
mv src/features/edit.disabled src/features/edit
```

### 17.3 调色回归矩阵

素材类型：

- PNG 8-bit
- JPEG
- TIFF 16-bit
- EXR/HDR，如果可用
- 视频帧，如果当前工作流支持

功能：

- 基础调色
- 曲线
- HSL 曲线
- LUT
- Mask
- Qualifier
- Soft Clip
- 插件
- 多图层
- 保存/重开
- 导出

---

## 18. 风险清单

### 18.1 高风险

- 修改 `AsyncRenderer` 线程和资源释放。
- 修改 `ShaderPipeline` GLSL 拼接顺序。
- 修改 `Layer` 序列化结构。
- 把 Color 从默认启用变成可关闭。
- 一次性移动大量文件。
- 删除旧 import 兼容层。

### 18.2 中风险

- SaveRouter handler 化。
- Composite Viewer 与 Canvas dock 交换逻辑。
- Edit 与 Color 的上下文切换。
- Composite 导出。
- 旧工程打开兼容。

### 18.3 低风险

- 新增文档。
- 新增 feature flag 数据类。
- lazy import Composite。
- 抽 shared.keyframe 并保留旧路径 re-export。
- 添加 Registry 框架但暂不接入所有模块。

---

## 19. 推荐提交粒度

建议每个提交只包含一个主题：

1. `docs: add modularization implementation plan`
2. `shared: add feature flag model`
3. `startup: guard composite plugin discovery`
4. `shared: move keyframe model out of composite`
5. `app: add feature registry skeleton`
6. `composite: register composite page through module`
7. `save: allow save handlers to be registered`
8. `color: add color facade without changing renderer`
9. `edit: register edit page through module`
10. `deliver: register deliver page through module`

---

## 20. 最小可行第一轮

如果只能做一轮，建议只做这三件事：

1. 新增 `src/shared/feature_flags.py`。
2. `src/main.py` 中 Composite 插件发现改为条件导入。
3. 把 `src.composite.keyframe` 抽到 `src.shared.keyframe`，旧路径保留兼容。

这一轮目标：

- 不碰调色核心。
- 不搬目录。
- 让 Composite 开始可关闭。
- 让 Edit 不再依赖 Composite 的 keyframe。

第一轮完成后的验证：

```bash
python -m compileall src
pytest tests/media
pytest tests/test_nodes
TOPOS_FEATURE_COMPOSITE=0 python src/main.py
```

人工验证：

- Color 正常。
- Edit 正常。
- Composite 开启正常。
- Composite 关闭后启动正常。

---

## 21. 最终验收标准

当整个计划完成时，应满足：

### 21.1 功能禁用

- `composite=false`：Composite 页、菜单、保存 handler、导出源全部禁用。
- `edit=false`：Edit 页、timeline、media pool、timeline 保存 handler 全部禁用。
- `deliver=false`：Deliver 页和导出入口禁用。
- Color 永远启用。

### 21.2 功能删除

- 删除 Composite 模块目录后，Color/Edit 可启动。
- 删除 Edit 模块目录后，Color 可启动。
- 删除 Deliver 模块目录后，Color/Edit/Composite 可启动。

### 21.3 调色稳定

- 调色参数兼容旧工程。
- 调色预览结果视觉一致。
- 调色导出结果视觉一致。
- GPU 资源无新增泄漏。
- UI 线程没有新增阻塞渲染调用。

### 21.4 架构边界

- `shared/` 不 import `features/`。
- `app/` 可以 import `shared/`，但不 import 功能内部深层模块，除非通过 module loader。
- `features/edit/` 不直接 import `features/composite/`。
- `features/composite/` 不直接 import `features/edit/`。
- 跨功能通信通过 contract/facade/registry。

---

## 22. 给后续 AI 的最后提醒

这次重构的成功标准不是“目录看起来整齐”，而是：

- 关闭一个功能，其它功能不坏。
- 删除一个功能目录，其它功能不坏。
- Color 作为核心调色管线保持稳定。

如果不确定某个改动是否会影响调色主线，默认不要改。  
先加接口，先加兼容层，先加验证，再考虑移动文件。

