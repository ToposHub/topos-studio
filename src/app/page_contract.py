from dataclasses import dataclass
from typing import Callable, Any, Protocol, List, Optional


@dataclass(frozen=True)
class PageDescriptor:
    """
    Descriptor for a page that can be registered with FeatureRegistry.
    
    Attributes:
        page_id: Unique identifier (e.g., "color", "edit", "composite")
        title: Display title for the page
        factory: Callable that creates the page widget
        default_enabled: Whether the page is enabled by default
        icon: Optional icon name for the page
        shortcut: Optional keyboard shortcut for switching to this page
    """
    page_id: str
    title: str
    factory: Callable[[Any], Any]
    default_enabled: bool = True
    icon: Optional[str] = None
    shortcut: Optional[str] = None
    
    def create_page(self, context: Any) -> Any:
        """Create the page widget using the factory."""
        return self.factory(context)


class FeatureModule(Protocol):
    """
    Protocol for feature modules that can be registered with FeatureRegistry.
    
    A feature module represents a major feature area of the application
    (e.g., Color, Edit, Composite, Deliver). Each module can:
    - Register pages that appear in the main window
    - Register actions (menu items, toolbar buttons)
    - Register save handlers for persisting feature-specific data
    
    Attributes:
        feature_id: Unique identifier for this feature
    """
    feature_id: str
    
    def is_available(self) -> bool:
        """
        Check if the feature module is available.
        
        Returns False if:
        - Feature flag is disabled
        - Module cannot be imported (missing dependencies)
        """
        ...
    
    def register_pages(self, context: Any) -> List[PageDescriptor]:
        """
        Register pages provided by this module.
        
        Args:
            context: AppContext instance
        
        Returns:
            List of PageDescriptor instances
        """
        ...
    
    def register_actions(self, context: Any) -> None:
        """
        Register actions (menu items, toolbar buttons) for this module.
        
        Args:
            context: AppContext instance
        """
        ...
    
    def register_save_handlers(self, context: Any) -> None:
        """
        Register save handlers for persisting feature-specific data.
        
        Args:
            context: AppContext instance with save_router
        """
        ...
    
    def on_page_enter(self, page_id: str, context: Any) -> None:
        """
        Called when entering a page belonging to this module.
        
        Args:
            page_id: The page being entered
            context: AppContext instance
        """
        ...
    
    def on_page_leave(self, page_id: str, context: Any) -> None:
        """
        Called when leaving a page belonging to this module.
        
        Args:
            page_id: The page being left
            context: AppContext instance
        """
        ...