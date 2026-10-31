# batch-365 审查报告 — src/shared/panels/

- 批次：batch-365 [P2]
- 编号区间：AUD-36500–AUD-36599
- 审查日期：2026-09-28
- 文件范围：`src/shared/panels/__init__.py` (10)、`src/shared/panels/registry.py` (267)

## src/shared/panels/__init__.py (行数: 10)

### 发现

（无）

### 通过项

- 通过（无逻辑）：纯 re-export 模块，从 `src.shared.panels.registry` 导出 `PanelDescriptor`、`PanelRegistry`、`get_panel_registry`、`validate_dock_integrity` 四个符号，`__all__` 与导入一致，无遗漏或多余。

## src/shared/panels/registry.py (行数: 267)

### 发现

- [AUD-36500] [Suggestion] register() 不校验 category/work_mode 枚举值，未知值导致面板被静默排除出 Window > Panels 菜单
  - 位置: src/shared/panels/registry.py:86
  - 证据:
    ```python
    def register(self, descriptor: PanelDescriptor) -> None:
        if descriptor.panel_id in self._panels:
            logger.warning(...)
            return
        self._panels[descriptor.panel_id] = descriptor
    ```
    以及 get_categories 的消费端过滤（registry.py:153）：
    ```python
    return [cat for cat in CATEGORY_ORDER if cat in grouped]
    ```
  - 影响: `register()` 只对重复 panel_id 告警，对 `category` 不在 CATEGORY_ORDER、`work_mode` 不在 {"all","image","video"} 的拼写错误照单全收。机制已核实：`get_panels_by_category` 会为未知类别建组，但菜单构建方（src/app/controllers/menu_toolbar_controller.py:1832）只遍历 `get_categories()` 返回的 CATEGORY_ORDER 内类别，未知类别的面板被静默丢弃，无任何日志；`work_mode` 拼错（如 "Image"）则面板在 image 和 video 两种过滤模式下都被隐藏（registry.py:141）。`validate_dock_integrity` 也只查 dock_attr/feature_id，不覆盖这两个枚举字段，且该守卫函数当前无生产调用方（仅 tests/ui/test_panel_dock_integrity.py 调用）。当前仓库内 4 个 feature 模块的注册值均合法，属潜在风险而非现行缺陷。
  - 建议: 在 `register()` 中校验 `category in CATEGORY_ORDER`（否则回退 DEFAULT_CATEGORY 并 logger.warning）与 `work_mode in ("all","image","video")`（否则回退 "all" 并告警）；或在 `validate_dock_integrity` 中补充这两项检查。
  - 置信度: 高

- [AUD-36501] [Suggestion] 未使用的导入 field/FrozenSet；typing 注解风格混用
  - 位置: src/shared/panels/registry.py:25-26
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Dict, List, Optional, Set, FrozenSet
    ```
    同时文件内另用 PEP 585/604 内建泛型（如 registry.py:31 `CATEGORY_ORDER: list[str]`、registry.py:176 `valid_features: set[str] | None`）。
  - 影响: `field` 与 `FrozenSet` 在全文无任何使用（dataclass 全部用简单默认值）；同一文件内 `Dict/List/Optional/Set`（typing 模块）与 `list[str]/dict[str,str]/set[str] | None`（内建泛型与 | 语法）并存，风格不一致，增加维护噪音。
  - 建议: 删除 `field`、`FrozenSet` 两个未用导入；统一为内建泛型风格（项目 requires-python >= 3.10，可全部改用 `dict/list/set` 与 `X | None`），或全部统一为 typing 模块风格。
  - 置信度: 高

- [AUD-36502] [Suggestion] validate_dock_integrity 的 window 参数缺类型注解；返回的 issues 混合错误与信息性条目
  - 位置: src/shared/panels/registry.py:175
  - 证据:
    ```python
    def validate_dock_integrity(
        registry: PanelRegistry,
        window,
        valid_features: set[str] | None = None,
    ) -> list[str]:
    ```
    以及信息性条目与错误条目同列表返回（registry.py:249-252）：
    ```python
    issues.append(
        f"Panel {desc.panel_id!r} shares dock_attr={desc.dock_attr!r} "
        f"with panel {first!r} (shared dock)"
    )
    ```
  - 影响: `window` 是全文唯一无类型注解的参数（文件其余部分注解完整），IDE/静态检查无法校验调用契约；"shared dock" 是明确标注为可接受的信息性条目，却与真正的错误返回在同一 `list[str]` 中且无结构化区分，自动调用方若以"非空即失败"处理会把合法的共享 dock 误报为问题。
  - 建议: 为 `window` 补注解（如 `TYPE_CHECKING` 下定义仅含 dock 属性 + toggleViewAction 的 Protocol，避免循环导入）；可将返回值结构化为 `(errors, warnings)` 或给条目附加严重级别枚举。
  - 置信度: 高

### 通过项

- 线程安全：所有调用点（menu_toolbar_controller、dock_layout_controller、color_workspace_controller 及各 feature 模块的 register_panels）均位于 UI 线程，注册集中在应用初始化阶段，未发现跨线程读写；纯 Python dict 的读写在此使用模式下无需加锁。
- GPU 资源生命周期：纯数据注册表，不涉及 ModernGL/OpenGL 资源，无泄漏风险。
- GLSL·HDR 正确性：不涉及着色器与像素数据，无 clamp/HDR 相关问题。
- 信号防递归：不持有/操作任何 Qt 控件与信号，仅消费 window 上的既有属性。
- 状态提交一致性：不涉及 undo/redo/revision；仅被 dock_layout_controller 只读取值用于布局持久化过滤，无状态写入。
- 异常与取消路径：`validate_dock_integrity` 全程使用 hasattr/getattr 防护不会抛出；`register`/`unregister_feature` 路径简单无异常源；`unregister_feature` 先收集后删除，避免迭代中修改字典。
- 输入验证（部分通过）：重复 panel_id 有告警并跳过、dock_attr 空值/缺失/无 toggleViewAction 均有检查；category/work_mode 枚举缺失见 AUD-36500。
- 性能模式：`CATEGORY_ORDER.index()` 位于排序 key 内为 O(n·m·log n)，但 n≈几十、m=6，且菜单构建为低频操作，实际无影响。
- 类型注解（部分通过）：除 AUD-36502 指出的 `window` 参数外，其余函数与 dataclass 字段注解完整，返回类型明确。
