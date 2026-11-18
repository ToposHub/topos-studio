"""Mixer workspace registration. The app hosts the shared project widgets."""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.app.page_contract import PageDescriptor
from src.shared.i18n.i18n import I18N
from src.shared.panels.registry import PanelDescriptor, PanelRegistry

if TYPE_CHECKING:
    from src.features.mixer.ui.mixer_page import MixerPage


MIXER_PANEL_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("console", "dock_audio_console", "console_panel", "Audio Mixer"),
    ("viewer", "dock_audio_viewer", "viewer_panel", "Viewport"),
    ("timeline", "dock_audio_timeline", "timeline_panel", "Audio Timeline"),
    ("clip_editor", "dock_audio_clip_editor", "clip_editor_panel", "Clip Editor"),
)


class MixerModule:
    """Register the Mixer workspace and its independent dock frames."""

    feature_id = "mixer"

    def __init__(self) -> None:
        self._page: MixerPage | None = None

    def is_available(self) -> bool:
        return True

    def register_pages(self, context: Any) -> list[PageDescriptor]:
        def create_mixer_page(page_context: Any) -> MixerPage:
            from src.features.mixer.ui.mixer_page import MixerPage
            if self._page is None:
                get_window = getattr(page_context, "get_main_window", None)
                if callable(get_window):
                    self._page = MixerPage(project_backed=True)
                else:
                    self._page = MixerPage()
            return self._page

        return [PageDescriptor(
            page_id="mixer", title=I18N.tr("page.mixer", "Mixer"),
            factory=create_mixer_page,
        )]

    def register_panels(self, registry: PanelRegistry) -> None:
        for order, (key, dock_attr, _panel_attr, title) in enumerate(MIXER_PANEL_SPECS, 100):
            title_key = {
                "console": "panel.audio_mixer", "viewer": "panel.edit_viewport",
            }.get(key, f"panel.audio_{key}")
            registry.register(PanelDescriptor(
                panel_id=f"mixer.{key}",
                title_key=title_key,
                fallback_title=title, feature_id="mixer", category="Mixer",
                page_ids=("Mixer",), dock_attr=dock_attr, order=order,
                default_visible=key != "clip_editor", work_mode="video",
            ))

    def register_i18n(self) -> None:
        I18N.load_module_translations(str(Path(__file__).parent / "locales"))

    def register_actions(self, context: Any) -> None:
        """The app page switcher and native dock actions provide UI navigation."""

    def register_save_handlers(self, context: Any) -> None:
        """Project changes persist through the shared Edit save handlers."""

    def on_page_enter(self, page_id: str, context: Any) -> None:
        if page_id == "mixer" and self._page is not None:
            self._page.on_enter()

    def on_page_leave(self, page_id: str, context: Any) -> None:
        if page_id == "mixer" and self._page is not None:
            self._page.on_leave()
