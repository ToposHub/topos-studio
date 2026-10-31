from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class AppContext:
    """
    Application context passed to feature modules during registration.
    
    Provides access to core application components that modules may need
    to register pages, actions, or save handlers.
    
    Attributes:
        main_window: MainWindow instance (shell for all pages)
        config_manager: ConfigManager instance
        feature_flags: FeatureFlags instance
        i18n: I18N translation utility
        logger: Application logger
        save_router: SaveRouter instance (optional, added later)
    """
    main_window: Any = None
    config_manager: Any = None
    feature_flags: Any = None
    i18n: Any = None
    logger: Any = None
    save_router: Optional[Any] = None
    
    def get_main_window(self) -> Any:
        return self.main_window
    
    def get_config(self) -> Any:
        return self.config_manager
    
    def get_feature_flags(self) -> Any:
        return self.feature_flags
    
    def is_feature_enabled(self, feature_id: str) -> bool:
        if self.feature_flags is None:
            return True
        return self.feature_flags.is_enabled(feature_id)