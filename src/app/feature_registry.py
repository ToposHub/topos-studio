import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from src.app.page_contract import PageDescriptor, FeatureModule
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


@dataclass
class FeatureRegistry:
    """
    Registry for feature modules and their pages.
    
    Central registry that manages feature modules and provides:
    - Page registration and discovery
    - Feature availability checking
    - Module lifecycle management
    
    The registry is created early in application startup and passed
    to MainWindow for page creation.
    """
    _modules: Dict[str, FeatureModule] = field(default_factory=dict)
    _pages: Dict[str, PageDescriptor] = field(default_factory=dict)
    _context: Optional[AppContext] = None
    
    def initialize(self, context: AppContext) -> None:
        """
        Initialize the registry with application context.
        
        Args:
            context: AppContext instance
        """
        self._context = context
    
    def register_module(self, module: FeatureModule) -> bool:
        """
        Register a feature module.
        
        Args:
            module: FeatureModule instance
        
        Returns:
            True if registration successful, False if module unavailable
        """
        if not module.is_available():
            logger.info(f"Feature module '{module.feature_id}' is not available, skipping registration")
            return False
        
        if module.feature_id in self._modules:
            logger.warning(f"Feature module '{module.feature_id}' already registered")
            return False
        
        self._modules[module.feature_id] = module
        logger.info(f"Registered feature module: {module.feature_id}")

        # Register i18n translations early, before pages/actions/menus
        if hasattr(module, 'register_i18n'):
            try:
                module.register_i18n()
            except Exception as e:
                logger.warning(f"Failed to register i18n for {module.feature_id}: {e}")

        if self._context:
            # [R2-4] 页面/动作注册与 i18n/ai_providers 同样加 try 隔离：
            # 单个模块的注册抛异常此前会中断整个注册链 → 应用启动 FATAL
            try:
                pages = module.register_pages(self._context)
                for page in pages:
                    self.register_page(page)
            except Exception as e:
                logger.warning(f"Failed to register pages for {module.feature_id}: {e}")

            try:
                module.register_actions(self._context)
            except Exception as e:
                logger.warning(f"Failed to register actions for {module.feature_id}: {e}")

            # Optional modules may expose AI context/command providers.  The
            # hook is deliberately capability-based so the base AI harness
            # does not import feature-specific code when the module is off.
            if hasattr(module, 'register_ai_providers'):
                try:
                    module.register_ai_providers(self._context)
                except Exception as e:
                    logger.warning(f"Failed to register AI providers for {module.feature_id}: {e}")

            if self._context.save_router:
                # [R2-4/AUD-3201] 与 pages/actions/ai_providers 同样的异常
                # 隔离：save_router 已存在时的动态注册路径上，单个模块的
                # register_save_handlers 抛异常不得沿注册链上抛（模块已在
                # _modules 中、pages 已注册 → 半注册状态 + 启动 FATAL）。
                try:
                    module.register_save_handlers(self._context)
                except Exception as e:
                    logger.warning(
                        f"Failed to register save handlers for "
                        f"{module.feature_id}: {e}"
                    )
        
        return True
    
    def register_page(self, page: PageDescriptor) -> None:
        """
        Register a page descriptor.
        
        Args:
            page: PageDescriptor instance
        """
        if page.page_id in self._pages:
            logger.warning(f"Page '{page.page_id}' already registered")
            return
        
        self._pages[page.page_id] = page
        logger.info(f"Registered page: {page.page_id} ({page.title})")
    
    def get_page(self, page_id: str) -> Optional[PageDescriptor]:
        """
        Get a registered page descriptor.
        
        Args:
            page_id: Page identifier
        
        Returns:
            PageDescriptor if found, None otherwise
        """
        return self._pages.get(page_id)
    
    def get_all_pages(self) -> List[PageDescriptor]:
        """
        Get all registered page descriptors.
        
        Returns:
            List of PageDescriptor instances (sorted by page_id)
        """
        return sorted(self._pages.values(), key=lambda p: p.page_id)
    
    def get_module(self, feature_id: str) -> Optional[FeatureModule]:
        """
        Get a registered feature module.
        
        Args:
            feature_id: Feature identifier
        
        Returns:
            FeatureModule if found, None otherwise
        """
        return self._modules.get(feature_id)
    
    def is_feature_available(self, feature_id: str) -> bool:
        """
        Check if a feature is available.
        
        Args:
            feature_id: Feature identifier
        
        Returns:
            True if feature is registered and available
        """
        module = self._modules.get(feature_id)
        if module is None:
            return False
        return module.is_available()
    
    def get_available_features(self) -> List[str]:
        """
        Get list of available feature IDs.
        
        Returns:
            List of feature identifiers
        """
        return [fid for fid, module in self._modules.items() if module.is_available()]
    
    def on_page_enter(self, page_id: str) -> None:
        """
        Notify modules when entering a page.
        
        Args:
            page_id: Page being entered
        """
        page = self._pages.get(page_id)
        if page is None:
            return
        
        # [AUD-3202] 运行期钩子分发与注册路径的 R2-4 隔离语义对齐：单个
        # 模块的 enter/leave 钩子抛异常不得中断迭代（后续模块收不到通知）
        # 或沿 switch_to_page 上抛使页面切换停在半途（离开侧布局未保存、
        # 进入侧 dock/视口未初始化）。
        for module in self._modules.values():
            try:
                module.on_page_enter(page_id, self._context)
            except Exception as e:
                logger.warning(
                    f"on_page_enter failed for {module.feature_id} "
                    f"on {page_id}: {e}"
                )
    
    def on_page_leave(self, page_id: str) -> None:
        """
        Notify modules when leaving a page.
        
        Args:
            page_id: Page being left
        """
        page = self._pages.get(page_id)
        if page is None:
            return
        
        # [AUD-3202] 同 on_page_enter：leave 钩子逐模块隔离。
        for module in self._modules.values():
            try:
                module.on_page_leave(page_id, self._context)
            except Exception as e:
                logger.warning(
                    f"on_page_leave failed for {module.feature_id} "
                    f"on {page_id}: {e}"
                )
    
    def register_all_save_handlers(self) -> None:
        """
        Register save handlers for all modules.
        
        Call this after AppContext.save_router is available.
        This is needed because modules are registered before MainWindow exists,
        so save_router wasn't available during initial registration.
        """
        if not self._context or not self._context.save_router:
            return
        
        for module in self._modules.values():
            try:
                module.register_save_handlers(self._context)
            except Exception as e:
                logger.warning(
                    f"Failed to register save handlers for {module.feature_id}: {e}"
                )

    def reload_i18n(self, locale_code: str) -> None:
        """
        Reload i18n for all registered modules after a locale switch.

        Args:
            locale_code: New locale code (e.g., "zh_CN", "en_US")
        """
        from src.shared.i18n.i18n import I18N
        I18N.reload_locale(locale_code)
        logger.info(f"Reloaded i18n for locale {locale_code} across {len(self._modules)} modules")
