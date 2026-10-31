# batch-367 [P2] src/shared/preferences/

- 审查日期: 2026-09-28
- 编号区间: AUD-36700–AUD-36799
- 审查文件: `src/shared/preferences/__init__.py`（158 行，包内唯一模块）
- 审查方式: 通读全文；对消费方 `src/app/ui/preferences_dialog.py`、`src/features/composite/render/disk_cache_store.py` 做只读交叉验证

## src/shared/preferences/__init__.py (行数: 158)

### 发现

- [AUD-36700] [Major] 包未导出消费方导入的 `get_config_manager`，磁盘缓存偏好设置全部静默失效
  - 位置: src/shared/preferences/__init__.py:15（本包全部导入区，无该符号）；消费方 src/features/composite/render/disk_cache_store.py:143、:465
  - 证据:
    ```python
    # __init__.py:15-19 —— 本包仅导入 dataclass/typing/PyQt6(TYPE_CHECKING)，无任何 re-export
    from dataclasses import dataclass, field
    from typing import Callable, Any, Optional, Protocol, TYPE_CHECKING

    # disk_cache_store.py:142-148 —— 消费方导入不存在的符号
    try:
        from src.shared.preferences import get_config_manager
        mode = str(get_config_manager().get(
            "composite.node_cache_format",
            "auto")).strip().lower()
    except Exception:  # noqa: BLE001 — 配置层缺失按 auto，不阻断缓存
        mode = "auto"
    ```
  - 影响: 全仓库 `def get_config_manager` 不存在（已 grep 验证，ConfigManager 实体在 `src/shared/config/config_manager.py`，各处直接 `ConfigManager()` 实例化）。该 ImportError 在两处消费点均被 `except Exception` 吞掉：1) `composite.node_cache_format`（Composite 偏好页有真实 UI 控件，preferences.py:47/233/244）永远走 "auto"，违反 disk_cache_store.py:133-137 注明"2026-09-19 审计收口"的模式来源优先级契约；2) `get_disk_cache_store()`（disk_cache_store.py:464-471）永远读不到 `composite.node_cache_dir` / `composite.node_cache_capacity_gb`，磁盘缓存目录与容量始终用默认值（20.0 GB）。用户可设置的偏好静默无效，且无任何日志指向根因。
  - 建议: 二选一并保持契约一致——在 `src/shared/preferences/__init__.py` 提供并导出 `get_config_manager()`（内部委托/缓存 `src.shared.config.config_manager.ConfigManager` 单例）；或将 disk_cache_store.py 两处改为直接导入 `ConfigManager`。修复后建议在两处 `except` 中对非 ImportError 异常补 `logger.warning`，避免同类静默降级不可诊断。
  - 置信度: 高（符号缺失与吞异常路径均已 Read 验证；偏好键确有 UI 入口）

- [AUD-36701] [Suggestion] `register()` 对重复 page_id 静默丢弃，无日志无告警
  - 位置: src/shared/preferences/__init__.py:120-121
  - 证据:
    ```python
    def register(self, descriptor: PreferencePageDescriptor) -> None:
        """Register a preference page descriptor."""
        if descriptor.page_id in self._pages:
            return  # Duplicate, ignore
        self._pages[descriptor.page_id] = descriptor
    ```
  - 影响: 两个 feature 意外撞 page_id（或插件重载携带新描述符重注册）时，后注册的页面静默从偏好对话框消失，无任何诊断线索。当前调用点（preferences_dialog.py:132-219）均为互异字面量 ID，属潜在风险而非现行缺陷。
  - 建议: 命中重复时 `logging.getLogger(__name__).warning(...)`；若两次描述符内容不同（title/factory/page_object 不一致）可直接抛 ValueError 暴露冲突。
  - 置信度: 高（行为确定；风险为潜在）

- [AUD-36702] [Suggestion] descriptor 允许 `factory` 与 `page_object` 同时为 None，故障推迟到 UI 构建期才暴露
  - 位置: src/shared/preferences/__init__.py:92-99；消费方 src/app/ui/preferences_dialog.py:270-273
  - 证据:
    ```python
    factory: Optional[Callable[[Any], Any]] = None
    ...
    page_object: Optional[Any] = None

    def __post_init__(self):
        """If page_object is set but no factory, create factory from page_object."""
        if self.page_object is not None and self.factory is None:
    ```
    ```python
    # preferences_dialog.py:270-273 —— desc.factory 为 None 时此处 TypeError
    if desc.page_object is not None:
        page_widget = desc.factory(self)
    else:
        page_widget = desc.factory()
    ```
  - 影响: 两者皆空的描述符可成功注册，但用户打开偏好对话框时抛 `TypeError: 'NoneType' object is not callable`，错误远离注册点，排查成本高。现有调用点均提供了 factory 或 page_object 之一，属潜在风险。同理，`page_object` 缺少 `create_widget` 的对象也要到 `desc.factory(self)` 调用时才 AttributeError。
  - 建议: `__post_init__` 末尾校验 `self.factory is None` 时抛 `ValueError`（factory/page_object 至少其一）；可用 `hasattr(self.page_object, "create_widget")` 做早失败检查。
  - 置信度: 高（行为确定；当前无触发调用点）

- [AUD-36703] [Suggestion] 类型注解缺失或弱化，`page_object` 丢失协议约束
  - 位置: src/shared/preferences/__init__.py:55、92、95、97、102、115
  - 证据:
    ```python
    def apply(self, config_manager: Any) -> None:          # :55 弱化为 Any
    factory: Optional[Callable[[Any], Any]] = None          # :92 参数/返回均为 Any
    page_object: Optional[Any] = None                       # :95 Optional[Any] 冗余且无约束
    def __post_init__(self):                                # :97 缺 -> None
    def _factory(parent=None):                              # :102 无注解
    def __init__(self):                                     # :115 缺 -> None
    ```
  - 影响: 本文件定义了 `ModularPreferencePage` 协议却不用于 `page_object` 类型，静态检查无法捕获未实现协议的页面对象（与 AUD-36702 的运行期 AttributeError 同根）；`apply()` 的 config_manager 为 Any，无法约束 `set()`/`save_config()` 接口；违反项目强类型注解约束。注: `field` 已导入（:15）但未使用，属冗余导入。
  - 建议: `page_object: Optional["ModularPreferencePage"] = None`；`factory: Optional[Callable[[Optional["QWidget"]], "QWidget"]] = None`；为 `apply` 的 config_manager 定义最小协议（含 set/save_config）；补齐 `-> None`；删除未用的 `field` 导入。
  - 置信度: 高

### 通过项

- 线程安全: 注册表在每次 `PreferencesDialog` 构建时于 UI 线程新建并填充（preferences_dialog.py:106/114/123-124），无跨线程共享，无锁需求
- GPU 资源生命周期: 本模块不持有任何 ModernGL/纹理/缓冲资源
- GLSL·HDR 正确性: 不涉及着色器与 HDR 数据通路
- 信号防递归: 模块内无 Qt 信号连接
- 状态提交一致性: 纯元数据注册表，不涉及 undo/redo/revision/持久化写入
- 性能模式: `get_pages` 的 O(n log n) 排序与 `get_categories` 的 O(n) 去重仅在对话框构建期调用一次，注册表规模约 10 项，无热点；工厂闭包 `page_obj` 捕获正确（无延迟绑定问题）
- `register`/`get_pages`/`get_page` 均为幂等只读访问，无变异共享状态逃逸
