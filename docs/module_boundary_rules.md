# Module Boundary Rules

## Layer Structure

```
src/
  app/           Application shell (MainWindow, feature registry, menu builder)
  shared/        Shared infrastructure (actions, panels, preferences, i18n, feature_flags)
  features/      Optional feature modules
    color/       Core feature (always enabled)
    edit/        Optional
    composite/   Optional
    deliver/     Optional
  products/      Product preset definitions and entry points
```

## Allowed Imports

### src/app can import from

- `src.shared.*` (any shared module)
- `src.products.*` (product presets, packaging config)
- Feature boundary modules only:
  - `src.features.edit.module`
  - `src.features.edit.commands_facade`
  - `src.features.composite.module`
  - `src.features.composite.facade`
  - `src.features.composite.export_provider`
  - `src.features.composite.runtime`
  - `src.features.deliver.module`
  - `src.features.deliver.facade`
  - `src.features.color.module`
  - `src.features.color.*` (color is always available)

### src/shared can import from

- Standard library
- Third-party packages (PyQt6, numpy, etc.)
- `src.shared.*` (other shared modules)
- `src.products.*` (product presets)

### src/shared MUST NOT import from

- `src.features.*` (any feature module)
- `src.app.*` (any app module)

### src/features/<feature> can import from

- `src.shared.*`
- `src.products.*`
- Other feature boundary modules (module.py, facade.py only)

### src/products can import from

- Standard library
- `src.products.*` (internal)
- `src.shared.feature_flags`

## Forbidden Imports

These patterns must NEVER appear in src/app or src/shared:

```
from src.features.edit.core.*
from src.features.edit.ui.*
from src.features.composite.core.*
from src.features.composite.render.*
from src.features.composite.adapters.*
from src.features.deliver.core.*
from src.features.deliver.ui.*
```

Enforced by `test_boundary_guardrails.py::TestAppLayerForbiddenImports`.

## Platform / Backend Code Ownership

平台/后端专属代码的归属规则（v2 GPU 迁移计划 §3.4 的裁定，此前本文档未规定；2026-09 由 P9D-03 落档）：

### src/shared/gpu/（后端无关层）

允许放置：backend 契约、能力探测、选择器、不引用 feature 对象的平台基础设施。

| 模块 | 定位 |
|---|---|
| `backend_types.py` / `capabilities.py` / `backend_selector.py` | 四维（graphics/compute/decode/interop）契约、能力矩阵与选择诊断 |
| `graphics_backend.py` | `GraphicsBackend` Protocol（具体实现在 feature 侧） |
| `gl_resource_registry.py` | GL 资源台账（纯记账，不做 GL 调用） |
| `metal/` | Metal compute 基础设施（`MetalComputeContext`、MSL kernel 集）——只依赖 PyObjC/numpy |
| `gl_backend.py` / `gl_context_hub.py` / `present_frame.py` / `gpu_scope_engine.py` | 既有共享设施，保持原样 |

### src/features/<feature>/（具体后端实现层）

需要包装 feature 侧 renderer / 资源对象（ModernGL 纹理、resource_pool、evaluator 管线）的具体后端实现**必须**放 feature 侧——放 `src/shared/` 会违反「shared 不得 import features」硬约束：

- `src/features/color/core/opengl_render_backend.py` — `GraphicsBackend` 的 OpenGL 实现（包装 `AsyncRenderer`）
- `src/features/composite/render/metal_compute_bridge.py` — GL 纹理 ↔ Metal compute 桥（知道 ModernGL 纹理与 resource_pool 契约）
- `compute_shader_manager.py` / `histogram_manager.py` 的 Metal 分支（与各自 GL 路径同文件，共享回退通道）

### 新增平台目录的默认归属

| 内容 | 归属 |
|---|---|
| 后端无关契约 / 类型 / 探测 | `src/shared/gpu/` |
| 平台 compute 基础设施（不触碰 feature 对象） | `src/shared/gpu/<platform>/`（如 `metal/`） |
| 包装 feature renderer / 资源池的具体后端实现 | 对应 feature 侧 |
| 平台解码 / interop（VideoToolbox、D3D11 等） | `src/shared/media/<platform>/`（与既有 `macos_vt/`、`windows_hw/` 同侧） |

## Whitelist (Approved Boundary Modules)

These are the only feature modules that src/app may import:

| Module | Purpose |
|---|---|
| `src.features.edit.module` | EditModule registration |
| `src.features.edit.commands_facade` | Edit command access |
| `src.features.composite.module` | CompositeModule registration |
| `src.features.composite.facade` | Composite object access |
| `src.features.composite.export_provider` | Composite export |
| `src.features.composite.runtime` | Composite runtime |
| `src.features.deliver.module` | DeliverModule registration |
| `src.features.deliver.facade` | Deliver page access |
| `src.features.color.module` | ColorModule registration (always available) |

## Enforcement

- **Static analysis**: `test_boundary_guardrails.py` scans source files for forbidden patterns
- **Import guards**: Feature modules use try/except ImportError around registration
- **Product presets**: `ALWAYS_ENABLED` enforces color/library in every product
- **Registry filtering**: ActionRegistry, PreferencesRegistry, PanelRegistry all accept `enabled_features` parameter
