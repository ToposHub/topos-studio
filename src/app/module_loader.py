"""[R2-3/R0-5] 特性模块注册的单一数据驱动实现。

历史上注册逻辑有三份平行实现：src/main.py 内联（唯一生产路径）、本文件
（DORMANT 休眠）、src/products/topos_*.py 的 register()（彼此 85-90% 同构
的死代码）。三份已实际漂移——ai_workflow 分支只有休眠副本有，生产启动
永不加载。

收敛后本文件是唯一注册实现：
- main.py 启动路径调用 register_color_module + register_builtin_modules
- 产品入口（src/products/topos_*.py）register() 委托
  register_preset_modules，按 preset 过滤同一张表
- 新增特性只需扩展 _FEATURE_MODULES 表
"""

import importlib
import logging
from dataclasses import dataclass

from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureModuleSpec:
    """注册表项：特性键 → 模块类，附可选的注册后钩子名。"""
    feature_id: str
    module_path: str
    class_name: str
    post_register_hook: str = ""


# 可选模块注册表（顺序即生产启动的注册顺序）。Color 是核心特性恒注册，
# 不在此表，走 register_color_module。新增特性只改此表。
_FEATURE_MODULES: tuple[FeatureModuleSpec, ...] = (
    FeatureModuleSpec(
        "composite", "src.features.composite.module", "CompositeModule",
        post_register_hook="discover_and_register_plugins",
    ),
    FeatureModuleSpec("edit", "src.features.edit.module", "EditModule"),
    FeatureModuleSpec("deliver", "src.features.deliver.module", "DeliverModule"),
    FeatureModuleSpec(
        "teaching_animation",
        "src.features.teaching_animation.module",
        "TeachingAnimationModule",
    ),
    FeatureModuleSpec(
        "ai_workflow", "src.features.ai_workflow.module", "AiWorkflowModule",
    ),
)


def _import_feature_class(spec: FeatureModuleSpec):
    module = importlib.import_module(spec.module_path)
    return getattr(module, spec.class_name)


def _register_spec(
    registry: FeatureRegistry, spec: FeatureModuleSpec
) -> tuple[bool, str]:
    """注册单个特性模块。

    返回 (是否成功, 失败原因)。ImportError/注册失败均隔离为 warning，
    不中断其余模块的注册链（与 i18n/ai_providers 的隔离语义一致）；
    失败原因由调用方收集用于 UI 提示（R2-8）。

    [AUD-3701] post_register_hook（如 composite 的
    discover_and_register_plugins）移出主 try 单独隔离：模块在钩子执行
    前就已注册进 registry（页面/动作可用），钩子抛异常只代表插件发现
    失败，不得返回 (False, error) 把"已注册"误报成"注册失败"——否则
    register_builtin_modules 把它记入 results=False 与 failures，
    main.py [R2-8] 据此向用户提示该特性加载失败，与实际状态相反。"""
    try:
        cls = _import_feature_class(spec)
        module = cls()
        if not registry.register_module(module):
            # register_module 返回 False = 模块不可用或重复注册——
            # 此时模块不在 registry 中，如实上报失败。
            logger.warning(
                "%s module registration rejected (unavailable or duplicate)",
                spec.feature_id)
            return False, "registration rejected (unavailable or duplicate)"
        logger.info("%s module registered", spec.feature_id)
    except ImportError as e:
        logger.warning("%s module unavailable; skipping: %s", spec.feature_id, e)
        return False, str(e)
    except Exception as e:
        logger.warning("%s registration failed: %s", spec.feature_id, e)
        return False, str(e)

    # 钩子在模块注册成功之后执行：失败不影响"已注册"的返回语义，
    # 仅降级为插件发现告警（页面/动作此时已可用）。
    if spec.post_register_hook and hasattr(module, spec.post_register_hook):
        try:
            count = getattr(module, spec.post_register_hook)()
            if count:
                logger.info("Loaded %s node plugins", count)
        except Exception as e:
            logger.warning(
                "%s registered, but post-register hook %r failed: %s",
                spec.feature_id, spec.post_register_hook, e)
    return True, ""


def create_registry(context: AppContext) -> FeatureRegistry:
    """
    Create and initialize the feature registry.

    Args:
        context: AppContext instance

    Returns:
        Initialized FeatureRegistry
    """
    registry = FeatureRegistry()
    registry.initialize(context)
    return registry


def register_color_module(registry: FeatureRegistry, *, guarded: bool = False) -> bool:
    """注册 Color 核心模块（恒注册，不受 feature flag 控制）。

    guarded=False（主启动路径）：导入/注册失败向上抛——Color 是核心特性，
    保持既有快速失败语义；guarded=True（产品入口）：仅捕获 ImportError，
    隔离为 warning 并返回 False。
    """
    try:
        from src.features.color.module import ColorModule
        registry.register_module(ColorModule())
        logger.info("Color module registered (core feature)")
        return True
    except ImportError as e:
        if guarded:
            logger.warning("Color module unavailable: %s", e)
            return False
        raise


def register_builtin_modules(
    registry: FeatureRegistry, flags=None, failures: dict | None = None
) -> dict[str, bool]:
    """按 feature flags 注册全部可选特性模块（生产启动的唯一注册路径）。

    Args:
        registry: 已 initialize 的 FeatureRegistry（flags 缺省时从
            ``registry._context.feature_flags`` 读取）。
        flags: 显式 FeatureFlags（可选，优先于 registry._context）。
        failures: [R2-8] 可选输出字典——flag 启用但导入/注册失败的模块
            ``{feature_id: 错误原因}``，供启动时 UI 提示。

    Returns:
        ``{feature_id: 是否注册成功}``（未启用/导入失败均为 False）。
    """
    if flags is None:
        if getattr(registry, "_context", None) is None:
            logger.warning("Registry not initialized, skipping module registration")
            return {}
        flags = registry._context.feature_flags

    results: dict[str, bool] = {}
    for spec in _FEATURE_MODULES:
        if not flags.is_enabled(spec.feature_id):
            logger.info(
                "%s feature is disabled; skipping registration", spec.feature_id
            )
            results[spec.feature_id] = False
            continue
        ok, error = _register_spec(registry, spec)
        results[spec.feature_id] = ok
        if not ok and failures is not None:
            failures[spec.feature_id] = error
    return results


def register_preset_modules(registry: FeatureRegistry, preset) -> None:
    """[R2-3] 产品入口统一注册：按 ``preset.resolved_enabled`` 过滤注册表。

    产品变体不再各自维护注册实现。Color 恒注册（guarded）；其余按 preset
    声明的启用集注册，不叠加 feature flags（与历史产品入口行为一致）。
    preset 未声明的特性（如 ai_workflow，非产品维度特性）不注册。
    """
    logger.info("[%s] registering modules from preset", preset.name)
    register_color_module(registry, guarded=True)
    for spec in _FEATURE_MODULES:
        if spec.feature_id in preset.resolved_enabled:
            _register_spec(registry, spec)


def discover_and_register_modules(registry: FeatureRegistry) -> None:
    """
    Discover and register all available feature modules.

    This is the main entry point for module registration during startup.

    Args:
        registry: FeatureRegistry instance
    """
    register_builtin_modules(registry)
    logger.info(
        f"Registered {len(registry.get_available_features())} feature modules"
    )
