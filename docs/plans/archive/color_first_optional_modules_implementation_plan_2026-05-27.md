# Color-First Optional Modules Implementation Plan (2026-05-27)

> 目标定位：Topos Color 的主产品定位是“调色软件”，类似 DaVinci Resolve 的 Color 页面优先思路。剪辑与合成是加分项，不应成为调色主路径的硬依赖。
>
> 总目标：保持 Color 作为核心必备能力，同时让 Edit、Composite、Deliver 逐步变成可关闭、可物理缺失、可独立开发、可按产品组合加载的模块。

---

## 0. 当前判断

当前项目已经完成了很大一部分模块化工作：

- `src/app/main_window.py` 已从 9000+ 行降到约 931 行。
- `src/features/color`、`src/features/edit`、`src/features/composite`、`src/features/deliver` 已经按 feature 目录分布。
- `FeatureFlags` 已存在，Edit/Composite/Deliver 已能通过 flag 做基本禁用。
- `tests/visual` 不再 segfault。
- `pytest -m color_core/composite_core/media_core/modular/visual` 已能真实选中测试。

但它还没有达到“随手删除 Edit 或 Composite 目录也没事”的最终状态。当前更准确的成熟度是：

```text
可关闭：基本具备
可物理缺失：接近，但还需要 hardening
可独立软件：还需要产品入口、协议抽象、打包配置
```

---

## 1. 总体架构目标

### 1.1 推荐目标结构

保持单仓库，不急着拆成多个 repo：

```text
apps/
  topos-color/          # 主产品：Color + 必要 shared + 可选 deliver
  topos-studio/         # 完整版：Color + Edit + Composite + Deliver
  topos-composite/      # 未来可选：Composite 专门入口

src/
  app/                  # App shell，不直接依赖 feature internals
  shared/               # 公共协议、媒体、GPU、项目、导出、UI 基础设施
  features/
    color/              # 核心功能，默认必开
    edit/               # 可选功能
    composite/          # 可选功能
    deliver/            # 可选功能，最终不应硬依赖 Edit
```

### 1.2 依赖方向

允许：

```text
app -> shared
app -> features.<feature>.module
features.<feature> -> shared
features.<feature> -> own core/ui/adapters
```

不推荐：

```text
app -> features.edit.core.*
app -> features.composite.core.*
deliver -> features.edit.core.*
edit -> features.color.core.*
composite -> features.color.core.*
shared -> features.*
```

特殊说明：

- Color 是核心，可以在短期保留少量 app -> color core 依赖，例如 `ImageEngine`。
- 但长期仍建议通过 `ColorRuntime` / `ColorFacade` / `ColorPipelineService` 收口。

---

## 2. 最终验收定义

### 2.1 Color-only 模式

关闭 Edit、Composite、Deliver 后，调色主程序仍能启动并通过核心测试：

```bash
TOPOS_DISABLE_FEATURES=edit,composite,deliver \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_color_session.py \
  tests/test_image_engine_state.py \
  -q
```

预期：

```text
全部通过
```

### 2.2 Edit 物理缺失

临时移走 Edit 文件夹，关闭 Edit/Deliver 后，Color-only 测试仍通过：

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_src_directory_boundaries.py \
  tests/test_color_session.py \
  -q
mv /tmp/topos_edit_disabled src/features/edit
```

预期：

```text
全部通过
```

### 2.3 Composite 物理缺失

```bash
mv src/features/composite /tmp/topos_composite_disabled
TOPOS_DISABLE_FEATURES=composite \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_src_directory_boundaries.py \
  tests/test_color_session.py \
  -q
mv /tmp/topos_composite_disabled src/features/composite
```

预期：

```text
全部通过
```

### 2.4 Studio 模式

所有模块开启时，已有核心测试仍通过：

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_src_directory_boundaries.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  tests/test_composite_preset.py \
  -q
```

---

## 3. 阶段路线图

| 阶段 | 名称 | 目标 | 风险 | 推荐粒度 |
|---|---|---|---|---|
| Phase 0 | 收尾当前尾巴 | 修 feature flag 优先级、cache、文档、预算 | 小 | 1 commit |
| Phase 1 | Color-only 启动模式 | 明确只加载 Color 的模式 | 小到中 | 1-2 commits |
| Phase 2 | Edit 可物理缺失 | 移走 `src/features/edit` 仍能跑 Color-only | 中 | 2-4 commits |
| Phase 3 | Deliver 去 Edit 硬依赖 | Deliver 不再直接 import Edit timeline export | 中 | 2-3 commits |
| Phase 4 | Composite 可物理缺失 | 移走 `src/features/composite` 仍能跑 Color-only | 中 | 2-4 commits |
| Phase 5 | 公共协议收口 | 抽 `FrameProvider`、`ExportSource`、`ProjectDocument` 等协议 | 中到大 | 多个小 commits |
| Phase 6 | 模块 UI 贡献系统 | 偏好设置、快捷键、多语言、菜单栏按模块注册 | 中到大 | 4-8 commits |
| Phase 7 | 多产品入口 | `topos-color` / `topos-studio` 不同启动入口 | 中 | 2-5 commits |
| Phase 8 | 独立打包准备 | 独立菜单、布局、配置、测试矩阵 | 大 | 分产品推进 |

---

## 4. Phase 0：先清掉当前尾巴

### 4.1 Feature flag 优先级

#### 问题

当前 `TOPOS_DISABLE_FEATURES` 会覆盖 `TOPOS_FEATURE_<NAME>`。如果未来 CI 或产品入口同时设置：

```bash
TOPOS_DISABLE_FEATURES=edit,composite
TOPOS_FEATURE_EDIT=true
```

结果会不直观。

#### 目标

优先级：

1. `TOPOS_FEATURE_<NAME>` 显式值优先。
2. `TOPOS_DISABLE_FEATURES` 次之。
3. config 值再次之。
4. 默认值最后。
5. `color` 永远 enabled。

#### 修改文件

```text
src/shared/feature_flags.py
tests/test_feature_flags.py
tests/test_feature_flags_registry.py
```

#### 验收

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py -q
```

手动验证：

```bash
TOPOS_FEATURE_EDIT=true TOPOS_DISABLE_FEATURES=edit \
.venv/bin/python - <<'PY'
from src.shared.feature_flags import load_feature_flags
assert load_feature_flags(None).edit is True
print("ok")
PY
```

#### 坑

- 不要允许 `TOPOS_FEATURE_COLOR=false` 真的关闭 Color。
- 不要让旧的 `src/app/feature_flags.py` wrapper 产生另一套逻辑。

---

### 4.2 清理旧 cache 残留

#### 问题

当前仍可能看到：

```text
.images_cache/
src/.images_cache/
```

它们大概率是旧运行残留，但会污染判断。

#### 修改文件

```text
.gitignore
tests/test_cache_fix.py
```

#### 操作

```bash
rm -rf .images_cache src/.images_cache
```

`.gitignore` 增加防御：

```gitignore
.images_cache/
src/.images_cache/
```

测试确保新路径不在 `src` 下：

```python
def test_cache_dir_not_under_src():
    from pathlib import Path
    from src.shared.io.file_io import FileIO

    cache_dir = Path(FileIO.CACHE_DIR).resolve()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    assert src_dir.resolve() not in cache_dir.parents
```

#### 验收

```bash
rm -rf .images_cache src/.images_cache
.venv/bin/python -m pytest tests/test_cache_fix.py tests/test_image_engine_state.py -q
test ! -d .images_cache
test ! -d src/.images_cache
git status --short
```

---

### 4.3 收紧 deep import budget

#### 问题

实际 app deep feature imports 已约 5 条，但测试预算仍是 100。

#### 修改文件

```text
tests/test_src_directory_boundaries.py
docs/validation_status_2026-05-27.md
```

#### 建议

先调到：

```python
MAX_APP_DEEP_FEATURE_IMPORTS = 8
```

后续每消掉一条再收紧：

```text
8 -> 5 -> 3 -> 0
```

#### 验收

```bash
.venv/bin/python -m pytest tests/test_src_directory_boundaries.py -q
```

#### 坑

- 不要为了数字把 import 全塞到函数里，造成真正依赖仍然存在，只是测试看不到。
- 更好的方向是 facade/adapter，而不是隐藏 import。

---

## 5. Phase 1：建立 Color-only 启动模式

### 5.1 目标

明确支持一种产品模式：

```text
Topos Color = Color + shared + optional Deliver
```

在这个模式下：

- 不注册 EditModule。
- 不注册 CompositeModule。
- 不创建 Edit/Composite 页面。
- 不创建 Edit/Composite docks。
- 菜单不显示 Edit/Composite 入口。
- 保存/导出逻辑不会访问 EditProject。

### 5.2 推荐实现

新增产品模式配置：

```text
TOPOS_PRODUCT_MODE=color
TOPOS_PRODUCT_MODE=studio
```

或在 config 中：

```json
{
  "product_mode": "color"
}
```

推荐优先级：

1. CLI arg：未来可以支持 `--product-mode=color`
2. env：`TOPOS_PRODUCT_MODE`
3. config
4. 默认：`studio`

### 5.3 修改文件

```text
src/main.py
src/app/app_context.py
src/app/module_loader.py
src/shared/feature_flags.py
tests/test_feature_flags_registry.py
tests/test_page_context.py
```

### 5.4 实现步骤

#### Step 1：增加 ProductMode 概念

可以先用简单字符串，不急着抽 enum：

```python
product_mode = os.environ.get("TOPOS_PRODUCT_MODE", "studio").lower()
```

Color mode 自动禁用：

```python
if product_mode == "color":
    flags.edit = False
    flags.composite = False
```

是否禁用 Deliver 可以分两种：

```text
color_minimal: color only
color: color + deliver
studio: all enabled
```

建议先做：

```text
color = Color + Deliver
studio = Color + Edit + Composite + Deliver
```

#### Step 2：写测试

```python
def test_color_product_mode_disables_edit_and_composite(monkeypatch):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
    flags = load_feature_flags(None)
    assert flags.color is True
    assert flags.edit is False
    assert flags.composite is False
```

#### Step 3：验证页面上下文

```bash
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest tests/test_page_context.py -q
```

### 5.5 验收命令

```bash
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_color_session.py \
  -q
```

### 5.6 坑

- `current_page` 可能保存为 Edit 或 Composite。Color mode 下必须 fallback 到 Color。
- 菜单/快捷键可能仍试图调用 Edit 方法。需要所有 action 创建都受 availability 控制。
- Dock restore 可能尝试恢复不存在的 panel。需要在 layout restore 前过滤 disabled page docks。

---

## 6. Phase 2：Edit 可物理缺失

### 6.1 目标

当 `src/features/edit` 不存在时，只要 Edit 被禁用，Color-only 程序仍可启动并通过测试。

### 6.2 当前已知断点

需要重点检查：

```text
src/app/main_window.py
src/app/controllers/file_controller.py
src/app/controllers/edit_controller.py
src/app/controllers/dock_layout_controller.py
src/app/controllers/page_controller.py
src/app/controllers/menu_toolbar_controller.py
src/features/deliver/ui/deliver_page.py
src/main.py
tests/media/test_video_frame_provider_service.py
tests/test_offline_asset_manager.py
tests/test_offline_relink_proxy.py
```

### 6.3 推荐策略

不要一开始删除 Edit。用临时物理缺失测试驱动：

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m pytest tests/test_page_context.py -q
mv /tmp/topos_edit_disabled src/features/edit
```

每发现一个 ImportError，就按“边界收口”修，而不是简单到处 `try/except`。

### 6.4 修改原则

#### 原则 1：app 只能依赖 EditModule 或 EditAdapter

允许：

```python
from src.features.edit.module import EditModule
```

或：

```python
from src.shared.project.edit_project_adapter import edit_project_adapter
```

不推荐：

```python
from src.features.edit.core.project import EditProject
from src.features.edit.ui.timeline_panel import TimelinePanel
```

#### 原则 2：Edit controller 应该只在 Edit 可用时混入或启用

当前 `MainWindow` 可能总是继承 `EditControllerMixin`。短期可以保留，但里面的方法必须：

```python
if not self._edit_available:
    return
```

长期更好：

```text
EditControllerMixin -> src/features/edit/adapters/main_window_edit_adapter.py
```

由 `EditModule` 注册。

#### 原则 3：EditProject 类型判断走 adapter

不要在 shared/app 里直接：

```python
isinstance(project, EditProject)
```

改用：

```python
edit_project_adapter.is_edit_project(project)
```

### 6.5 推荐拆解任务

#### Task 2.1：MainWindow 顶层 edit import 清零

目标：

```bash
rg -n "from src\\.features\\.edit|import src\\.features\\.edit" src/app/main_window.py
```

只允许在函数内部、且被 `_edit_available` 保护。

验收：

```bash
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

#### Task 2.2：FileController 去 Edit 硬依赖

当前 `file_controller.py` 顶层有 EditProjectIO / EditProject / TimelineSequence 等条件导入。短期可接受，但物理缺失时必须安全。

建议：

- 把 Edit project save/load 逻辑移动到 `src/features/edit/adapters/project_file_adapter.py`。
- `FileController` 通过 adapter 调用。
- Edit 不存在时，adapter 返回 unavailable。

验收：

```bash
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

物理缺失验收：

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
mv /tmp/topos_edit_disabled src/features/edit
```

#### Task 2.3：菜单与快捷键隐藏 Edit actions

检查：

```bash
rg -n "_edit_available|Edit|Timeline|Media Pool" src/app/controllers/menu_toolbar_controller.py
```

要求：

- Edit disabled 时不创建 Edit 菜单。
- Edit disabled 时不注册 Edit 快捷键。
- Edit disabled 时触发旧 action 不崩，只提示 unavailable 或 no-op。

验收：

```bash
TOPOS_DISABLE_FEATURES=edit \
.venv/bin/python -m pytest tests/test_page_context.py -q
```

#### Task 2.4：测试分类

Edit 物理缺失时，以下测试不应参与 Color-only gate：

```text
tests/edit/
tests/visual/test_edit_page_visual.py
tests/test_offline_asset_manager.py
tests/test_offline_relink_proxy.py
```

建议 marker：

```python
pytestmark = pytest.mark.edit_core
```

或在 `pytest_collection_modifyitems` 根据路径自动标记。

Color-only gate 应 ignore 或 deselect：

```bash
.venv/bin/python -m pytest -m "not edit_core" ...
```

### 6.6 Phase 2 最终验收

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m compileall -q src tests
TOPOS_DISABLE_FEATURES=edit,deliver \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_src_directory_boundaries.py \
  tests/test_color_session.py \
  -q
mv /tmp/topos_edit_disabled src/features/edit
```

### 6.7 坑

- `compileall src tests` 会编译 `tests/edit`，如果 edit 目录已移走，测试文件中的 import 仍可能失败是在 runtime，不是 compile time；不要误以为 compileall 能覆盖。
- Deliver 当前依赖 Edit timeline export。Edit 缺失时最好同时禁用 Deliver，直到 Phase 3 修完。
- Python import cache 会影响物理缺失测试。最好新开进程运行 pytest，不要在同一个 Python 进程里移动目录后 import。

---

## 7. Phase 3：Deliver 去 Edit 硬依赖

### 7.1 问题

当前 Deliver UI 直接导入：

```python
from src.features.edit.core.timeline_export import ...
```

这意味着：

```text
删除 Edit -> Deliver import 失败
```

如果 Topos Color 主产品仍想保留 Deliver 页面用于导出单张图、调色结果、批量 LUT，那么 Deliver 不能硬依赖 Edit。

### 7.2 目标

Deliver 支持多种 export source：

```text
ColorExportSource
TimelineExportSource
CompositeExportSource
```

其中 TimelineExportSource 只有 Edit 可用时注册。

### 7.3 推荐结构

新增 shared export 协议：

```text
src/shared/export/export_source.py
src/shared/export/export_registry.py
```

接口草案：

```python
from typing import Protocol, Iterable

class ExportSource(Protocol):
    source_id: str
    title: str

    def is_available(self) -> bool:
        ...

    def estimate_frames(self) -> int:
        ...

    def render_frame(self, frame_index: int):
        ...
```

Color 注册：

```text
src/features/color/adapters/export_source.py
```

Edit 注册：

```text
src/features/edit/adapters/timeline_export_source.py
```

Composite 注册：

```text
src/features/composite/adapters/export_source.py
```

Deliver 只读 registry：

```python
from src.shared.export.export_registry import get_export_sources
```

### 7.4 迁移步骤

#### Step 1：把 timeline_export 的通用类型抽到 shared

可能移动：

```text
ExportCodec
ExportRangeType
TimelineExportConfig
```

到：

```text
src/shared/export/types.py
```

注意：如果类型名字强绑定 Timeline，可以先保留，不急着全移动。

#### Step 2：Deliver import 改成 try adapter

短期最小修：

```python
try:
    from src.features.edit.core.timeline_export import create_timeline_exporter
    EDIT_EXPORT_AVAILABLE = True
except ImportError:
    create_timeline_exporter = None
    EDIT_EXPORT_AVAILABLE = False
```

但这只是止血。长期要 registry。

#### Step 3：Deliver UI 根据 source availability 展示

Edit 不可用时：

- 不显示 Timeline export source。
- 保留 Color frame export。
- 不崩溃。

### 7.5 验收

Edit disabled，但 Deliver enabled：

```bash
TOPOS_DISABLE_FEATURES=edit \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_save_router.py \
  tests/test_export_frame_provider.py \
  -q
```

Edit 物理缺失，但 Deliver enabled：

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_save_router.py \
  -q
mv /tmp/topos_edit_disabled src/features/edit
```

### 7.6 坑

- 不要把 Edit timeline export 整个搬到 shared，如果里面仍强依赖 Edit 数据模型。先抽协议，不抽实现。
- Deliver 页面初始化不要因为缺少 Timeline exporter 失败。
- 旧项目如果保存的是 timeline export preset，需要降级提示，而不是 crash。

---

## 8. Phase 4：Composite 可物理缺失

### 8.1 目标

当 `src/features/composite` 不存在时，只要 Composite 被禁用，Color-only 程序仍通过测试。

### 8.2 当前重点检查

```bash
rg -n "src\\.features\\.composite" src/app src/shared src/features/color src/features/edit src/features/deliver -g '*.py'
```

关注：

```text
src/app/main_window.py
src/app/controllers/composite_controller.py
src/app/controllers/dock_layout_controller.py
src/app/controllers/file_controller.py
src/features/edit/core/project.py
src/features/deliver/*
src/shared/ai_models/*
```

### 8.3 推荐策略

Composite 是可选加分项，应该通过：

```text
CompositeModule
CompositeRuntime
CompositeGraphFacade
CompositeExportSource
```

与 app 或 other features 通信。

### 8.4 实施步骤

#### Task 4.1：Composite imports 全部 availability guard

要求 app 侧所有 composite import 都满足：

```python
if self._composite_available:
    from src.features.composite...
```

并且 ImportError 时：

```python
self._composite_available = False
```

验收：

```bash
TOPOS_DISABLE_FEATURES=composite \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

#### Task 4.2：Composite adapter 注册

把 Composite 与 shared/AI/cache 的连接变成注册式 callback。

当前 `CompositeModule` 已有 `_register_ai_cache_callback()`，这是正确方向。

继续检查 shared 是否仍反向 import composite：

```bash
rg -n "src\\.features\\.composite" src/shared -g '*.py'
```

目标：

```text
src/shared -> composite imports: 0
```

#### Task 4.3：物理缺失测试

```bash
mv src/features/composite /tmp/topos_composite_disabled
TOPOS_DISABLE_FEATURES=composite \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_src_directory_boundaries.py \
  tests/test_color_session.py \
  -q
mv /tmp/topos_composite_disabled src/features/composite
```

### 8.5 坑

- Composite 可能被项目加载、保存、export、AI cache、node plugin discovery 间接触发。
- 启动时不要无条件 discover composite plugins。
- 如果当前页面保存为 Composite，禁用后必须 fallback Color。

---

## 9. Phase 5：公共协议收口

### 9.1 为什么需要协议

如果目标只是“目录整齐”，现在已经很接近了。但如果目标是：

```text
独立开发
独立测试
独立产品入口
又能合并成 Studio
```

就必须靠公共协议，而不是各 feature 互相 import 内部类型。

### 9.2 推荐协议清单

| 协议 | 放置位置 | 解决什么问题 |
|---|---|---|
| `FrameProvider` | `src/shared/video/frame_provider_protocol.py` | Edit/Composite/Color 都能请求帧 |
| `RenderSource` | `src/shared/render/source.py` | 导出或 viewer 读取渲染源 |
| `ExportSource` | `src/shared/export/export_source.py` | Deliver 不依赖 Edit |
| `ProjectDocument` | `src/shared/project/document.py` | 保存路由不依赖 EditProject |
| `ColorPipelineFacade` | `src/features/color/facade.py` | 其他模块使用调色输出 |
| `CompositeGraphFacade` | `src/features/composite/facade.py` | 其他模块使用合成结果 |
| `TimelineFacade` | `src/features/edit/facade.py` | 其他模块使用时间线，不碰 Edit core |

### 9.3 实施顺序

#### Step 1：先定义协议，不搬实现

例如：

```python
class ProjectDocument(Protocol):
    document_type: str
    dirty: bool

    def save_to_path(self, path: str) -> None:
        ...
```

#### Step 2：让现有实现适配协议

```text
EditProjectAdapter(ProjectDocument)
ColorSessionAdapter(ProjectDocument)
CompositeGraphAdapter(ProjectDocument)
```

#### Step 3：app/shared 只认协议

保存、导出、最近项目、离线媒体 relink 都通过协议。

### 9.4 坑

- 不要过早抽巨型 `FeatureService`。协议越小越好。
- 不要把 UI 类型放进 shared 协议。
- 不要让 shared import PyQt widget，shared 应尽量保持纯逻辑。

---

## 10. Phase 6：模块 UI 贡献系统

### 10.1 为什么这一步很重要

偏好设置、快捷键、多语言、菜单栏是模块化最容易“表面拆开、实际绑死”的地方。

如果这些仍由 app 壳硬编码：

```text
PreferencesDialog 直接写死 Edit/Composite 设置页
MenuToolbarController 直接创建 Edit/Composite 菜单项
ShortcutManager 只在菜单创建时顺手注册快捷键
I18N 只加载一个全局语言包
```

那么即使 `src/features/edit` 目录结构很漂亮，产品上仍然不是可选模块。真正目标应是：

```text
App Shell 提供容器
Feature Module 注册贡献项
Shared Registry 统一收集
UI 根据当前启用模块动态生成
```

最终效果：

```text
Color-only:
  菜单只显示 app/shared/color/deliver 相关
  偏好只显示 General/Appearance/Color/Export/Advanced
  快捷键只显示当前启用模块
  i18n 只加载 shared + color + deliver

Studio:
  菜单显示 Color/Edit/Composite/Deliver
  偏好显示所有模块
  快捷键显示所有模块
  i18n 加载所有模块语言包
```

### 10.2 推荐新增基础设施

建议新增：

```text
src/shared/actions/
  __init__.py
  action_descriptor.py
  action_registry.py

src/shared/preferences/
  __init__.py
  page_descriptor.py
  preferences_registry.py

src/shared/i18n/
  contribution.py
  module_loader.py

src/app/menu/
  menu_builder.py
```

每个 feature module 增加贡献接口：

```python
class FeatureModule:
    def register_preferences(self, registry): ...
    def register_shortcuts(self, registry): ...
    def register_menu_actions(self, registry): ...
    def register_i18n(self, registry): ...
```

短期可以不改 `FeatureModule` 抽象类，只在 `ColorModule` / `EditModule` / `CompositeModule` / `DeliverModule` 上约定这些方法，app shell 用 `hasattr()` 调用。等稳定后再收进正式接口。

### 10.3 偏好设置拆分方案

#### 当前问题

`src/app/ui/preferences_dialog.py` 当前是单体式，固定写死：

```text
General
Appearance
Color & Processing
Performance
Export
Shortcuts
File & Storage
Advanced
```

这会导致：

- Edit disabled 时，仍可能出现 Edit/Timeline/Playback 相关偏好。
- Composite disabled 时，仍可能出现 Composite cache/render 相关偏好。
- 未来 Topos Color 独立产品和 Studio 产品无法自然拥有不同偏好页。

#### 目标结构

偏好设置变成 registry-driven：

```python
@dataclass(frozen=True)
class PreferencePageDescriptor:
    page_id: str
    title_key: str
    title_default: str
    category: str
    order: int
    feature_id: str | None
    factory: Callable[[PreferenceContext], QWidget]
```

基础 app/shared 注册：

```text
app.general
app.appearance
app.file_storage
app.advanced
```

Color 注册：

```text
color.processing
color.gpu
color.cache
color.ai_models
```

Edit 注册：

```text
edit.timeline
edit.playback
edit.audio
edit.media_pool
```

Composite 注册：

```text
composite.node_graph
composite.cache
composite.viewer
composite.render
```

Deliver 注册：

```text
deliver.export
deliver.codecs
deliver.locations
```

#### 实施步骤

1. 新增 `PreferencePageDescriptor` 和 `PreferencesRegistry`。
2. 先把 General / Appearance / Advanced 包成 app/shared descriptors。
3. `PreferencesDialog` 改为从 registry 生成左侧类别和右侧 page。
4. 把 Color & Processing 页迁移到 `ColorModule.register_preferences()`。
5. 把 Export 页迁移到 `DeliverModule.register_preferences()`。
6. 最后迁移 Edit / Composite 相关页。
7. Edit/Composite disabled 时，对应 descriptors 不注册或被过滤。

#### 验收命令

```bash
TOPOS_DISABLE_FEATURES=edit,composite \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

建议新增测试：

```text
tests/test_preferences_registry.py
```

覆盖：

```python
def test_edit_preferences_hidden_when_edit_disabled(): ...
def test_composite_preferences_hidden_when_composite_disabled(): ...
def test_color_preferences_always_present(): ...
def test_preference_page_ids_are_unique(): ...
```

#### 坑

- 偏好页 factory 不要在注册时创建 QWidget，必须延迟到用户打开偏好页时再创建。
- 偏好页不要启动 renderer、解码线程、AI model 下载。
- Apply/Cancel 要支持每个模块页回滚，不能一个模块 Apply 后另一个模块 Cancel 状态混乱。
- 预览类设置，例如 UI scale、accent color，要有 snapshot 机制，Cancel 时能还原。
- 不要让 `PreferencesDialog` import `src.features.edit.ui.*` 或 `src.features.composite.ui.*`。

### 10.4 快捷键拆分方案

#### 当前基础

`src/shared/ui/shortcut_manager.py` 已经是中心注册器，这是好的基础。问题是快捷键注册往往发生在菜单创建时，容易让菜单、快捷键、模块生命周期绑在一起。

#### 目标结构

新增 shortcut descriptor：

```python
@dataclass(frozen=True)
class ShortcutDescriptor:
    action_id: str
    label_key: str
    label_default: str
    category_key: str
    category_default: str
    default_shortcut: str
    feature_id: str | None
```

ID 命名规则：

```text
app.file.open
app.preferences.open
app.window.fullscreen

color.grade.reset
color.viewer.fit
color.layer.add
color.lut.import

edit.project.new
edit.timeline.cut
edit.timeline.ripple_delete
edit.media.import

composite.node.add
composite.node.delete
composite.viewer.fit

deliver.export.start
deliver.export.cancel
```

#### 实施步骤

1. 新增 `ShortcutDescriptor`。
2. `ShortcutManager.register()` 支持从 descriptor 注册。
3. app/shared 注册基础快捷键。
4. 每个 feature module 注册自己的 shortcuts。
5. 菜单 action 通过 `shortcut_id` 绑定，而不是菜单代码自己决定快捷键。
6. 快捷键对话框只显示当前启用模块的 shortcuts。
7. 用户自定义 shortcut 不要因模块禁用而删除，只是 inactive。

#### 验收命令

```bash
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_page_context.py -q
```

建议新增：

```text
tests/test_shortcut_registry.py
```

覆盖：

```python
def test_edit_shortcuts_hidden_when_edit_disabled(): ...
def test_disabled_module_custom_shortcuts_are_preserved(): ...
def test_shortcut_ids_are_unique(): ...
def test_active_shortcut_conflicts_detected_only_for_enabled_modules(): ...
```

#### 坑

- 不要删除 disabled module 的用户自定义快捷键，否则用户重新启用模块会丢配置。
- 冲突检测要区分 active conflicts 和 inactive conflicts。
- 同一个快捷键在 Color-only 和 Studio 里可能含义不同，要定义产品模式优先级。
- `Ctrl+S` 这类全局快捷键必须由 app shell 决策当前保存路由，不要由 Edit/Color 分别抢。
- 快捷键 label/category 要走 i18n，不要硬编码 `"Edit"`、`"Color"`。

### 10.5 多语言系统拆分方案

#### 当前问题

当前 I18N 是集中 JSON 加载。模块化后如果所有翻译都放在一个全局文件里，会有几个问题：

- 删除 Edit 目录后仍残留 Edit 翻译 key。
- feature 独立开发时要改 shared 语言包，边界不清。
- key 冲突不容易发现。

#### 目标结构

基础语言包：

```text
src/shared/i18n/locales/en_US.json
src/shared/i18n/locales/zh_CN.json
```

模块语言包：

```text
src/features/color/i18n/en_US.json
src/features/color/i18n/zh_CN.json
src/features/edit/i18n/en_US.json
src/features/edit/i18n/zh_CN.json
src/features/composite/i18n/en_US.json
src/features/composite/i18n/zh_CN.json
src/features/deliver/i18n/en_US.json
src/features/deliver/i18n/zh_CN.json
```

加载顺序：

```text
1. shared/app 基础语言包
2. 当前启用模块语言包
3. fallback locale
4. 冲突检测和 warning
```

key 命名规则：

```text
app.menu.file
app.preferences.title

color.panel.layers
color.action.reset_grade
color.preferences.processing

edit.panel.timeline
edit.action.cut
edit.preferences.playback

composite.panel.node_graph
composite.action.add_node
composite.preferences.cache

deliver.action.export
deliver.preferences.codec
```

#### 实施步骤

1. 给 I18N 增加 `register_locale_dir(feature_id, path)`。
2. `FeatureModule.register_i18n(registry)` 注册自己的语言目录。
3. `I18N.load_locale()` 加载 shared 后合并 enabled module 的语言包。
4. key 冲突时 warning，并显示冲突来源。
5. 语言切换时重新加载当前启用模块语言包。
6. 菜单、偏好页、快捷键对话框收到语言变化信号后刷新文本。

#### 验收命令

```bash
TOPOS_DISABLE_FEATURES=edit,composite \
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_page_context.py -q
```

建议新增：

```text
tests/test_i18n_contributions.py
```

覆盖：

```python
def test_disabled_module_i18n_not_required(): ...
def test_enabled_module_i18n_loaded(): ...
def test_i18n_key_conflicts_are_detected(): ...
def test_missing_module_locale_falls_back_to_shared(): ...
```

#### 坑

- 禁用模块时不要强制读取它的 i18n 目录，否则物理缺失测试会失败。
- key 冲突要报警，不要静默覆盖。
- fallback 语言包也要按模块加载，否则部分模块 fallback 不完整。
- 切换语言不能要求重启所有模块，但短期可以提示部分 UI 需要重开窗口。

### 10.6 菜单栏拆分方案

#### 当前问题

`src/app/controllers/menu_toolbar_controller.py` 当前集中创建菜单，并且会直接创建 Edit Project、Color、View、Preferences 等 action。模块可选后，这会变成强耦合点。

#### 目标结构

新增 menu/action contribution：

```python
@dataclass(frozen=True)
class MenuActionDescriptor:
    action_id: str
    menu_path: str
    label_key: str
    label_default: str
    shortcut_id: str | None
    order: int
    feature_id: str | None
    callback_name: str
    enabled_when: str | None = None
```

示例：

```python
MenuActionDescriptor(
    action_id="edit.project.new",
    menu_path="File/Edit Project",
    label_key="edit.action.new_project",
    label_default="New Edit Project...",
    shortcut_id="edit.project.new",
    order=200,
    feature_id="edit",
    callback_name="new_edit_project",
)
```

App shell 基础菜单：

```text
File
Edit
View
Window
Help
```

ColorModule 贡献：

```text
Color / Reset Grade
Color / LUT / Import LUT
View / Scopes
Layer / Add Layer
```

EditModule 贡献：

```text
File / Edit Project / New
File / Edit Project / Open
Edit / Timeline / Cut
Edit / Timeline / Ripple Delete
```

CompositeModule 贡献：

```text
Composite / Add Node
Composite / Render Preview
View / Composite Viewer
```

DeliverModule 贡献：

```text
File / Export
Deliver / Start Export
Deliver / Open Output Folder
```

#### 实施步骤

1. 新增 `ActionRegistry` 和 `MenuBuilder`。
2. 先把 app 基础 File/Open/Save/Preferences/Exit 迁移成 descriptors。
3. 把 Edit Project 菜单项迁移到 `EditModule.register_menu_actions()`。
4. 把 Composite 菜单项迁移到 `CompositeModule.register_menu_actions()`。
5. 把 Color 相关菜单项迁移到 `ColorModule.register_menu_actions()`。
6. `MenuToolbarMixin.setup_menus()` 变成：

```python
descriptors = action_registry.get_menu_actions(enabled_features)
MenuBuilder(self, self.shortcut_manager, I18N).build(menu_bar, descriptors)
```

#### 验收命令

```bash
TOPOS_DISABLE_FEATURES=edit,composite \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

建议新增：

```text
tests/test_menu_contributions.py
```

覆盖：

```python
def test_edit_menu_actions_hidden_when_edit_disabled(): ...
def test_color_menu_actions_present_in_color_mode(): ...
def test_menu_action_ids_are_unique(): ...
def test_menu_callbacks_exist_on_main_window_or_registered_handler(): ...
def test_disabled_feature_menu_does_not_import_feature_ui(): ...
```

#### 坑

- descriptor 不要持有 QWidget、QAction 或 panel 实例，只保存描述。
- callback 可以是 `callback_name`，构建 QAction 时再绑定到 main window 或 feature handler。
- 当前页面相关动作要动态 enable/disable，不要只在菜单构建时判断一次。
- macOS native menu bar 和 Qt menuBar 行为不同。项目当前禁用了 native menuBar，后续要保持一致。
- 不要让 `MenuBuilder` import `src.features.edit.*`。
- 菜单 order 必须稳定，否则不同模块加载顺序会导致菜单位置乱跳。

### 10.7 Toolbar / Dock / Layout 也要走贡献机制

菜单拆完后，还会遇到类似问题：

```text
工具栏按钮
Dock 面板列表
默认布局
窗口菜单里的 Panel 开关
欢迎页 / 新建项目入口
状态栏组件
```

建议后续继续扩展 descriptors：

```python
ToolbarActionDescriptor
DockPanelDescriptor
DefaultLayoutDescriptor
StatusBarWidgetDescriptor
NewProjectTemplateDescriptor
```

不要一开始全做，推荐顺序：

```text
MenuActionDescriptor
ShortcutDescriptor
PreferencePageDescriptor
I18nContribution
DockPanelDescriptor
ToolbarActionDescriptor
```

### 10.8 Phase 6 最终验收

Color-only：

```bash
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_preferences_registry.py \
  tests/test_shortcut_registry.py \
  tests/test_menu_contributions.py \
  tests/test_i18n_contributions.py \
  -q
```

Studio：

```bash
TOPOS_PRODUCT_MODE=studio \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_preferences_registry.py \
  tests/test_shortcut_registry.py \
  tests/test_menu_contributions.py \
  tests/test_i18n_contributions.py \
  -q
```

Physical missing smoke：

```bash
mv src/features/edit /tmp/topos_edit_disabled
trap 'mv /tmp/topos_edit_disabled src/features/edit' EXIT
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest \
  tests/test_menu_contributions.py \
  tests/test_preferences_registry.py \
  tests/test_shortcut_registry.py \
  tests/test_i18n_contributions.py \
  -q
```

### 10.9 Phase 6 常见失败模式

| 失败模式 | 典型症状 | 修复方向 |
|---|---|---|
| 菜单构建时 import disabled feature | Edit 物理缺失后启动失败 | descriptor 延迟绑定，不在 MenuBuilder 中 import feature |
| disabled shortcut 被删除 | 用户重新启用模块后快捷键丢失 | 自定义快捷键按 action_id 持久化，inactive 不显示但保留 |
| i18n key 冲突 | 不同模块同名 key 文案互相覆盖 | 强制 key namespace，冲突 warning |
| 偏好页过早创建 QWidget | 打开 Preferences 前就触发 PyQt/GL/AI 初始化 | factory lazy create |
| Apply/Cancel 状态混乱 | 一个模块预览后取消影响另一个模块 | 每个 preference page 实现 snapshot/apply/revert |
| 菜单 order 不稳定 | 不同启动时菜单顺序变化 | descriptor 必须有稳定 order |
| 当前页面 action 未刷新 | 切换页面后 action enabled 状态错误 | 引入 action state refresh hook |
| 产品模式和 feature flag 打架 | Color mode 下 Edit 菜单仍出现 | 产品模式先给默认 flags，显式 env 再覆盖 |
| 语言切换不刷新菜单 | 切语言后菜单仍显示旧语言 | I18N 发 signal，MenuBuilder rebuild 或 relabel |
| Dock layout 引用 disabled panel | restore layout 时找不到 dock object | layout restore 过滤 disabled feature docks |

---

## 11. Phase 7：多产品入口

### 11.1 目标

同一个代码库启动不同产品：

```bash
python -m apps.topos_color
python -m apps.topos_studio
```

或：

```bash
python src/main.py --product color
python src/main.py --product studio
```

### 11.2 推荐入口

短期：

```text
src/main.py --product color
src/main.py --product studio
```

长期：

```text
apps/topos_color/main.py
apps/topos_studio/main.py
```

### 11.3 Color 产品默认启用

```text
color: enabled
deliver: enabled
edit: disabled
composite: disabled
```

### 11.4 Studio 产品默认启用

```text
color: enabled
deliver: enabled
edit: enabled
composite: enabled
```

### 11.5 独立默认布局

Color mode：

```text
Viewer
Layer Panel
Grading Panel
Scopes
LUT / Presets
Deliver optional
```

Studio mode：

```text
Library
Edit
Color
Composite
Deliver
```

### 11.6 验收

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest tests/test_page_context.py -q
TOPOS_PRODUCT_MODE=studio .venv/bin/python -m pytest tests/test_page_context.py -q
```

如果有 GUI smoke：

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python src/main.py --test-startup
TOPOS_PRODUCT_MODE=studio .venv/bin/python src/main.py --test-startup
```

### 11.7 坑

- 产品模式不要和 feature flag 打架。产品模式设置默认 feature flags，显式 env 仍可覆盖。
- 配置文件可能需要按产品分开：`topos-color/config.json` vs `topos-studio/config.json`。
- 最近打开项目也可能按产品分开，否则 Color mode 会尝试打开 Studio 项目。

---

## 12. Phase 8：独立开发与测试矩阵

### 12.1 Marker 建议

已有：

```text
color_core
composite_core
media_core
visual
modular
```

建议新增：

```text
edit_core
deliver_core
color_product
studio_product
physical_missing
```

### 12.2 CI gate 建议

#### Color-only gate

```bash
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest \
  -m "color_core or media_core or modular" \
  --ignore=tests/edit \
  --ignore=tests/visual \
  -q
```

#### Studio gate

```bash
TOPOS_PRODUCT_MODE=studio \
.venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_color_session.py \
  tests/test_composite_graph.py \
  tests/edit \
  -q
```

#### Physical missing gate

先不要放默认 CI，放手动或 nightly：

```bash
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_PRODUCT_MODE=color \
.venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py -q
mv /tmp/topos_edit_disabled src/features/edit
```

### 12.3 坑

- 物理移动目录的测试不能和并行测试同时跑。
- 物理缺失 gate 必须保证 teardown 恢复目录。
- 最好写 shell 脚本用 `trap` 恢复：

```bash
mv src/features/edit /tmp/topos_edit_disabled
trap 'mv /tmp/topos_edit_disabled src/features/edit' EXIT
...
```

---

## 13. 给 AI 的推荐任务模板

### 12.1 修一个 import 断点

```text
你正在 Topos Color 项目中做 Color-first optional module hardening。

目标：
让 Edit disabled / Edit physical missing 时，Color-only 测试仍通过。

约束：
1. 不改调色核心算法。
2. 不改变现有 UI 行为。
3. 不做大规模重构。
4. 每次只处理一个 import 断点。
5. app/shared 不得新增对 src.features.edit.core/ui 的硬依赖。

请先运行：
rg -n "src\\.features\\.edit" src/app src/shared src/features/deliver -g '*.py'

然后选择一个断点修复，最后验证：
TOPOS_DISABLE_FEATURES=edit,deliver .venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

### 12.2 做 Edit 物理缺失验证

```text
请验证 src/features/edit 物理缺失时 Color-only 模式是否可运行。

要求：
1. 使用 mv 临时移走目录。
2. 使用 trap 或手动确保恢复目录。
3. 不删除用户文件。
4. 记录第一个失败 import。
5. 只修复必要边界，不做额外重构。

验收：
mv src/features/edit /tmp/topos_edit_disabled
TOPOS_DISABLE_FEATURES=edit,deliver .venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py -q
mv /tmp/topos_edit_disabled src/features/edit
```

### 12.3 抽 Deliver export protocol

```text
目标：
让 Deliver 不再硬依赖 src.features.edit.core.timeline_export。

约束：
1. 不移动 timeline_export 实现到 shared，除非只移动纯类型。
2. 新增 shared/export ExportSource 协议。
3. Edit 可用时注册 TimelineExportSource。
4. Edit 不可用时 Deliver 页面仍可初始化。
5. 保持现有导出测试通过。

验证：
TOPOS_DISABLE_FEATURES=edit .venv/bin/python -m pytest tests/test_page_context.py tests/test_save_router.py tests/test_export_frame_provider.py -q
```

---

## 14. 推荐提交顺序

### Commit 1：Current tail cleanup

内容：

- feature flag precedence
- cache residue ignore/test
- validation docs number update
- deep import budget tighten

验证：

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest tests/test_feature_flags.py tests/test_feature_flags_registry.py tests/test_src_directory_boundaries.py tests/test_cache_fix.py -q
```

### Commit 2：Color product mode

内容：

- `TOPOS_PRODUCT_MODE=color/studio`
- Color mode disables Edit/Composite by default
- page fallback tests

验证：

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py -q
TOPOS_PRODUCT_MODE=studio .venv/bin/python -m pytest tests/test_page_context.py -q
```

### Commit 3：Edit physical missing hardening 1

内容：

- remove app/main_window top-level edit runtime dependencies
- guard menu/page/dock edit paths

验证：

```bash
TOPOS_DISABLE_FEATURES=edit,deliver .venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

### Commit 4：Edit physical missing hardening 2

内容：

- FileController / SaveRouter Edit project adapter cleanup
- tests for Edit unavailable

验证：

```bash
mv src/features/edit /tmp/topos_edit_disabled
trap 'mv /tmp/topos_edit_disabled src/features/edit' EXIT
TOPOS_DISABLE_FEATURES=edit,deliver .venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py -q
```

### Commit 5：Deliver export decoupling

内容：

- shared export source protocol
- Deliver no hard import of Edit timeline exporter
- Edit registers timeline export source

验证：

```bash
TOPOS_DISABLE_FEATURES=edit .venv/bin/python -m pytest tests/test_page_context.py tests/test_save_router.py tests/test_export_frame_provider.py -q
```

### Commit 6：Composite physical missing hardening

内容：

- composite imports guarded
- shared reverse imports remain zero
- physical missing smoke

验证：

```bash
mv src/features/composite /tmp/topos_composite_disabled
trap 'mv /tmp/topos_composite_disabled src/features/composite' EXIT
TOPOS_DISABLE_FEATURES=composite .venv/bin/python -m pytest tests/test_page_context.py tests/test_color_session.py -q
```

### Commit 7：UI contribution registries

内容：

- `PreferencePageDescriptor` / `PreferencesRegistry`
- `ShortcutDescriptor` 注册流程
- `MenuActionDescriptor` / `MenuBuilder`
- I18N module locale contribution
- Color-only / Studio 下的菜单、偏好、快捷键、语言包过滤测试

验证：

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest \
  tests/test_preferences_registry.py \
  tests/test_shortcut_registry.py \
  tests/test_menu_contributions.py \
  tests/test_i18n_contributions.py \
  -q

TOPOS_PRODUCT_MODE=studio .venv/bin/python -m pytest \
  tests/test_preferences_registry.py \
  tests/test_shortcut_registry.py \
  tests/test_menu_contributions.py \
  tests/test_i18n_contributions.py \
  -q
```

---

## 15. 不建议做的事

### 15.1 不建议立刻拆仓库

原因：

- 当前 shared 协议还没完全稳定。
- 多仓库会显著增加同步成本。
- AI 在跨 repo 任务中更容易漏上下文。

建议等到：

```text
Edit physical missing gate 通过
Composite physical missing gate 通过
Deliver 不硬依赖 Edit
Color product mode 可启动
```

再考虑多仓库。

### 15.2 不建议先拆 Color

Color 是主产品核心，不应先动它的根。

优先顺序：

```text
先让 Edit/Composite optional
再让 Deliver protocol-based
最后再整理 Color facade
```

### 15.3 不建议靠 try/except 到处吞错误

`try/except ImportError` 是过渡方案，不是最终架构。

最终应该是：

```text
feature module registers capability
app shell consumes capability
shared defines protocol
```

---

## 16. 成功状态

当这份计划完成后，Topos Color 会具备：

- 作为纯调色软件发布的能力。
- Edit/Composite 作为可选加分模块存在。
- Studio 模式可以组合加载所有模块。
- Edit/Composite 可以独立开发和测试。
- Deliver 不再被 Edit 绑死。
- 偏好设置、快捷键、多语言、菜单栏按启用模块动态贡献。
- Color-only 与 Studio 可以拥有不同菜单、默认布局、快捷键集合和偏好页。
- AI 后续修改时有明确边界，不容易把模块重新搅在一起。

最终产品形态可以是：

```text
Topos Color      = Color + Deliver
Topos Studio     = Color + Edit + Composite + Deliver
Topos Composite  = Composite + shared media/render + optional Color source
```

但短期最重要的一步不是做三个软件，而是让：

```text
Color-only 模式可信
Edit/Composite 可关闭且可物理缺失
跨模块依赖全部走协议
```
