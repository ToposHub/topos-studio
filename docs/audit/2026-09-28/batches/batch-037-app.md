# batch-037 审查报告 [P1] src/app/

批次：batch-037（AUD-3700–AUD-3799）
文件：src/app/module_loader.py (181 行)、src/app/page_contract.py (103 行)
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/app/module_loader.py (行数: 181)

### 发现

- [AUD-3701] [Major] post_register_hook 抛异常时模块已注册成功，`_register_spec` 却返回 (False, error)，状态与报告矛盾
  - 位置: src/app/module_loader.py:70-82
  - 证据:
    ```python
    registry.register_module(module)
    logger.info("%s module registered", spec.feature_id)
    if spec.post_register_hook and hasattr(module, spec.post_register_hook):
        count = getattr(module, spec.post_register_hook)()
        if count:
            logger.info("Loaded %s node plugins", count)
    return True, ""
    except Exception as e:
        logger.warning("%s registration failed: %s", spec.feature_id, e)
        return False, str(e)
    ```
  - 影响: `FeatureRegistry.register_module`（feature_registry.py:55）在页面/动作注册前就已把模块放入 `self._modules`，且其内部 try 只隔离 pages/actions 失败后仍返回 True。因此当 composite 的 `discover_and_register_plugins` 钩子（本文件 72-75 行）抛出任何异常时，走 80 行的宽 except 返回 `(False, error)`——模块实际已注册、页面可用，但 `register_builtin_modules` 把它记入 `results=False` 与 `failures` 字典，main.py [R2-8] 路径据此向用户提示"该特性加载失败"，与实际状态相反；同时插件的失败被错误归类为"注册失败"。
  - 建议: 将钩子调用移出主 try，单独 try 隔离并区分"已注册但插件发现失败"与"导入/注册失败"两种返回语义（例如返回三态或 `(True, hook_error)`）。
  - 置信度: 高

- [AUD-3702] [Suggestion] `register_preset_modules` 丢弃 `_register_spec`/`register_color_module` 的失败结果，产品入口无 R2-8 失败提示
  - 位置: src/app/module_loader.py:163-166
  - 证据:
    ```python
    register_color_module(registry, guarded=True)
    for spec in _FEATURE_MODULES:
        if spec.feature_id in preset.resolved_enabled:
            _register_spec(registry, spec)
    ```
  - 影响: `_register_spec` docstring（64-66 行）明确"失败原因由调用方收集用于 UI 提示（R2-8）"，但四个产品入口（src/products/topos_*.py）走的这条路径完全忽略返回值、也不接收 failures——preset 声明启用的特性导入失败时，产品变体静默缺页，仅剩日志 warning，用户无可感知提示。
  - 建议: 仿照 `register_builtin_modules` 增加 `failures` 输出参数并透传给产品入口做启动提示。
  - 置信度: 高

- [AUD-3703] [Suggestion] `create_registry` 与 `discover_and_register_modules` 为全库无调用的死代码，与"唯一注册实现"的收敛意图相悖
  - 位置: src/app/module_loader.py:85-97, 169-181
  - 证据:
    ```python
    def create_registry(context: AppContext) -> FeatureRegistry:
        ...
        registry = FeatureRegistry()
        registry.initialize(context)
        return registry
    ...
    def discover_and_register_modules(registry: FeatureRegistry) -> None:
        ...
        register_builtin_modules(registry)
        logger.info(
            f"Registered {len(registry.get_available_features())} feature modules"
        )
    ```
  - 影响: 全库 grep 仅命中两处定义，无任何调用（main.py:229-234 直接 `FeatureRegistry()` + `initialize(app_context)`）。文件 docstring 声称"收敛后本文件是唯一注册实现"，但保留的旧入口（含不收集 failures 的旧签名和 f-string 急切格式化日志）会随时间再次漂移——这正是本文件开头描述的历史问题（三份实现漂移）的复发土壤。
  - 建议: 删除这两个函数，或让 main.py 改用 `create_registry` 收敛构造路径。
  - 置信度: 高

- [AUD-3704] [Suggestion] `register_color_module` 忽略 `register_module` 的布尔返回值，注册未生效时仍返回 True 并打 "registered" 日志
  - 位置: src/app/module_loader.py:108-111
  - 证据:
    ```python
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    logger.info("Color module registered (core feature)")
    return True
    ```
  - 影响: `FeatureRegistry.register_module`（feature_registry.py:47-53）在模块不可用或 feature_id 重复注册时返回 False 且不注册；此处无条件记日志并返回 True。当前 `ColorModule.is_available()` 恒 True（src/features/color/module.py:47-56），实际触发场景主要是重复注册（如产品入口与主路径叠加误用）时误报成功、调用方无从感知缺页。
  - 建议: 接收返回值 `ok = registry.register_module(ColorModule())`，失败时按 guarded 语义返回 False 并修正日志条件。
  - 置信度: 高（事实成立）/ 低（当前实际触发概率）

- [AUD-3705] [Suggestion] 类型注解缺失（flags/preset/_import_feature_class 返回值），且跨模块读 `registry._context` 私有字段
  - 位置: src/app/module_loader.py:54, 120, 134-138, 155
  - 证据:
    ```python
    def _import_feature_class(spec: FeatureModuleSpec):
    ...
    def register_builtin_modules(
        registry: FeatureRegistry, flags=None, failures: dict | None = None
    ) -> dict[str, bool]:
    ...
        if getattr(registry, "_context", None) is None:
            ...
        flags = registry._context.feature_flags
    ...
    def register_preset_modules(registry: FeatureRegistry, preset) -> None:
    ```
  - 影响: `flags` 实为 `src.shared.feature_flags.FeatureFlags`、`preset` 实为 `src.products.ProductPreset`，均未标注，静态检查失效；更值得注意的是 135 行用 `getattr(registry, "_context", None)` 防御式访问私有字段——若 FeatureRegistry 未来重命名 `_context`，此路径静默落入 "Registry not initialized" warning 并返回 `{}`，全部可选模块静默不注册，属于改名即静默失效的脆弱耦合。
  - 建议: 补齐类型注解；FeatureRegistry 暴露 public `context`/`feature_flags` 属性供外部读取。
  - 置信度: 高

### 通过项
- 线程安全：纯启动期单线程调用（main.py:229-253 / products 入口），无可变共享状态，无并发风险
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据
- 信号防递归：无 Qt 信号连接
- 状态提交一致性：不涉及 undo/redo/revision/持久化（注册顺序由 `_FEATURE_MODULES` 元组固定，数据驱动表无重复 feature_id）
- 异常与取消路径：`_register_spec` 的 ImportError/Exception 分级隔离有 docstring 说明并收集失败原因（与 i18n/ai_providers 隔离语义一致，非掩盖；钩子误归类问题见 AUD-3701）；`register_color_module` guarded=False 时 ImportError 正确向上抛保持快速失败
- 输入验证：`_FEATURE_MODULES` 为编译期常量内部表；`preset.resolved_enabled`（frozenset）与 `flags.is_enabled`（src/shared/feature_flags.py:177-183）均为内部类型已验证
- 性能模式：importlib 按需导入、无热路径，无性能问题

---

## src/app/page_contract.py (行数: 103)

### 发现

- [AUD-3706] [Suggestion] `factory: Callable[[Any], Any]` 弱类型，context 与页面 widget 均失去静态检查
  - 位置: src/app/page_contract.py:2, 20, 25-27
  - 证据:
    ```python
    from typing import Callable, Any, Protocol, List, Optional
    ...
        factory: Callable[[Any], Any]
    ...
        def create_page(self, context: Any) -> Any:
            """Create the page widget using the factory."""
            return self.factory(context)
    ```
  - 影响: 实际语义是 context 为 AppContext、返回值为页面 widget，全部标 Any 后 feature_registry/main_window 侧对 descriptor 的使用完全没有类型保护；`register_pages(self, context: Any) -> List[PageDescriptor]` 等协议方法同样如此。另 `List/Optional` 为旧式 typing 导入，与同目录 module_loader.py 的 `dict | None` 现代风格不一致（app_context.py 仅 import dataclasses/typing，收紧注解不会引入循环导入）。
  - 建议: 将 factory 与协议方法收紧为 `Callable[[AppContext], QWidget]`（或最小 Protocol），统一 `list[...]`/`X | None` 现代写法。
  - 置信度: 高（事实）/ 中（改进收益）

### 通过项
- 通过（无运行时逻辑）：文件仅含 frozen dataclass `PageDescriptor`（不可变、字段带默认值，`create_page` 为工厂直通转发）与结构性 `FeatureModule` Protocol（方法体全为 `...`），无控制流可审
- 线程安全：不可变数据定义，天然线程安全
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常路径：不适用（纯类型契约层）
- 输入验证：`create_page` 直接转发 factory，factory 属内部注册方提供（feature_registry.register_page 已去重 page_id），无需额外验证
- 性能模式：无热路径
