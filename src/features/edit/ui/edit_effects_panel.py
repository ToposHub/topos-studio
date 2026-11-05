"""
Edit Effects Panel - Effects library and application.

This panel displays available video effects, transitions, audio effects, and
Motion Graphic video generators.
Users can browse, search, and apply effects to clips in the timeline.

Features:
- Category-based browsing (Video Effects, Transitions, Audio)
- Motion Graphic generator recipes with hover GIF previews
- Search filtering
- Double-click to apply effect to selected clip
- Drag & drop to timeline clips
- Favorites system with star button on each effect

Architecture:
- Uses EffectRegistry to get effect definitions
- Emits signals for effect application
- Independent of timeline data (UI only)
- Favorites stored in ConfigManager

Drag and Drop Support:
- Effects can be dragged from the effects tree to the timeline
- Drop on a clip adds the effect to that clip
- Transitions can be dragged to cut points between clips

P7 Completion:
- Audio Effects tab fully implemented
- Favorites system with persistence
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional, Set, Dict, Any, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTreeWidget, QTreeWidgetItem, QLineEdit, QTabWidget, QSizePolicy,
    QAbstractItemView, QToolButton, QGridLayout, QApplication, QMenu,
    QFormLayout, QSpinBox,
)
from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
    pyqtSlot,
    QMetaObject,
    QMimeData,
    QByteArray,
    QSize,
    QPoint,
    QRect,
    QEvent,
    QThread,
    QTimer,
)
from PyQt6.QtGui import QIcon, QDrag, QMovie, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import (
    icon_favorite,
    icon_favorite_outline,
    icon_search,
    IconColors,
)
from src.shared.config.config_manager import ConfigManager
from src.shared.ui.style_sheet import C
from src.shared.ui.dock_context_menu import (
    add_dock_management_actions,
    find_parent_dock,
)
from src.features.edit.core.effect_registry import (
    GENERATOR_EFFECT_TYPES,
    NON_ATTACHABLE_VIDEO_EFFECT_TYPES,
    get_effect_registry,
    EffectDefinition,
)
from src.features.edit.core.transition_catalog import TRANSITION_DEFINITIONS
from src.features.edit.core.audio_transition_catalog import (
    AUDIO_TRANSITION_DEFINITIONS,
    is_audio_transition,
)
from src.features.teaching_animation.core.motion_graphic_generator_registry import (
    MOTION_GRAPHIC_GENERATOR_MIME,
    get_motion_graphic_generator_registry,
)
from src.features.teaching_animation.core.content_localization import (
    template_description,
    template_display_name,
)

logger = logging.getLogger(__name__)

_TRANSITION_ICON_DIR = Path(__file__).with_name("transition_icons")
_TRANSITION_PREVIEW_DIR = Path(__file__).with_name("transition_previews") / "generated"
_MAX_ACTIVE_TRANSITION_PREVIEWS = 24
_TRANSITION_PREVIEW_SIZE_OPTIONS = {
    # (GIF width, GIF height, card height)
    "small": (96, 54, 78),
    "medium": (128, 72, 100),
    "large": (192, 108, 136),
}
_DEFAULT_TRANSITION_PREVIEW_SIZE = "medium"
_EFFECT_ICON_DIR = Path(__file__).with_name("effect_icons")
_EFFECT_BG = C.BG_PANEL
_EFFECT_BORDER = C.BORDER
_EFFECT_BORDER_SUBTLE = C.BORDER_DARK
_EFFECT_TEXT = C.TEXT_DIM
_TITLE_PRESET_ROLE = int(Qt.ItemDataRole.UserRole)
_CAPTION_EFFECT_ROLE = _TITLE_PRESET_ROLE + 1
# AUD-19809: 标签页索引具名常量（addTab 顺序：0=Video、1=Transitions），
# 上下文菜单不再用魔法数字判断当前是否转场页。
_TAB_INDEX_TRANSITIONS = 1

_EFFECT_TREE_STYLE = f"""
QTreeWidget {{
    background: {_EFFECT_BG};
    border: none;
    color: {_EFFECT_TEXT};
    font-size: 11px;
    outline: none;
}}
QTreeWidget::viewport {{ background: {_EFFECT_BG}; }}
QTreeWidget::item {{ border: none; padding: 0px; }}
QTreeWidget::item:selected {{ background: transparent; color: {_EFFECT_TEXT}; }}
QTreeWidget::branch:selected {{ background: transparent; }}
QTreeWidget::branch:!active {{ background: transparent; }}
QScrollBar:vertical {{ width: 8px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {_EFFECT_BORDER_SUBTLE}; min-height: 24px; border-radius: 2px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
"""


@lru_cache(maxsize=512)
def _effect_preview_icon(effect_type: str) -> QIcon:
    """Load the Resolve-style SVG preview for an effect type.

    Plugin IDs contain a colon, which is intentionally mapped to ``__`` in
    filenames so the asset directory remains portable across platforms.

    AUD-19803: the rasterized icon is cached per effect_type. A single tree
    rebuild previously rasterized the same SVG twice per effect (tree item +
    card) and re-``stat``-ed the file each time; QIcon is implicitly shared,
    so returning a cached instance is cheap and safe.
    """
    filename = effect_type.replace(":", "__").lower() + ".svg"
    path = _EFFECT_ICON_DIR / filename
    return _dimmed_svg_icon(path) if path.is_file() else icon_favorite_outline()


def _dimmed_svg_icon(path: Path, opacity: float = 0.62) -> QIcon:
    """Render an SVG into a quiet monochrome icon for the compact library UI."""
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return QIcon()
    pixmap = QPixmap(48, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setOpacity(opacity)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


@lru_cache(maxsize=128)
def _transition_preview_icon(transition_type: str) -> QIcon:
    path = _TRANSITION_ICON_DIR / f"{transition_type}.svg"
    if not path.is_file():
        path = _TRANSITION_ICON_DIR / "generic_transition.svg"
    return _dimmed_svg_icon(path)


class _EffectPreviewButton(QToolButton):
    """Resolve-style effect card used inside a responsive category grid."""

    double_clicked = pyqtSignal(str)
    drag_requested = pyqtSignal(str)
    context_menu_requested = pyqtSignal(str, object)

    def __init__(
        self,
        effect_type: str,
        name: str,
        description: str,
        icon: QIcon,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.effect_type = effect_type
        self._press_pos: Optional[QPoint] = None
        self._drag_started = False
        self.setText(name)
        self.setIcon(icon)
        self.setIconSize(QSize(40, 24))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setToolTip(f"{name}\n{description}")
        self.setFixedHeight(31)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QToolButton {{"
            f" background: {_EFFECT_BG}; border: 1px solid {_EFFECT_BORDER_SUBTLE};"
            f" border-radius: 1px; color: {_EFFECT_TEXT}; padding: 1px 4px;"
            " font-size: 11px; font-weight: 400; text-align: left; }"
            f"QToolButton:hover {{ background: {_EFFECT_BG};"
            f" border-color: {_EFFECT_BORDER}; color: {C.TEXT_SECONDARY}; }}"
            f"QToolButton:pressed {{ background: {_EFFECT_BG}; }}"
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._emit_context_menu)

    def _emit_context_menu(self, position: QPoint) -> None:
        self.context_menu_requested.emit(
            self.effect_type,
            self.mapToGlobal(position),
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._drag_started = True
            self.drag_requested.emit(self.effect_type)
            self._press_pos = None
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.effect_type)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _MotionGraphicPreviewButton(_EffectPreviewButton):
    """An MG card that plays its packaged GIF while the pointer is over it."""

    _PREVIEW_SIZE = QSize(320, 180)

    def __init__(
        self,
        effect_type: str,
        name: str,
        description: str,
        preview_path: Path | None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            effect_type,
            name,
            description,
            _effect_preview_icon("motion_graphic"),
            parent,
        )
        self._preview_path = Path(preview_path) if preview_path else None
        self._preview_popup: QLabel | None = None
        self._preview_movie: QMovie | None = None
        if self._preview_path is not None and self._preview_path.is_file():
            movie = QMovie(str(self._preview_path))
            if movie.isValid() and movie.jumpToFrame(0):
                self.setIcon(QIcon(movie.currentPixmap()))

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._show_preview()

    def leaveEvent(self, event: QEvent) -> None:
        self._hide_preview()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        # AUD-19801: ``QTreeWidget.clear()`` (search / favorites / registry
        # refresh) deletes the cards without ever delivering ``leaveEvent``
        # to them. The hide event is the last reliable hook on that
        # destruction path, so use it to close the hover preview.
        self._hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._hide_preview()
        super().closeEvent(event)

    def _show_preview(self) -> None:
        if self._preview_popup is not None or self._preview_path is None:
            return
        # AUD-19801: the popup and movie must be owned by this card. A
        # parentless ToolTip window and QMovie survive the card's deletion —
        # the window keeps showing and decoding its GIF until the Python
        # wrapper is collected (non-deterministic). As children they are
        # destroyed together with the card.
        movie = QMovie(str(self._preview_path), parent=self)
        if not movie.isValid():
            return
        movie.setScaledSize(self._PREVIEW_SIZE)
        popup = QLabel(self, Qt.WindowType.ToolTip)
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        popup.setStyleSheet(
            f"background: {_EFFECT_BG}; border: 1px solid {_EFFECT_BORDER}; padding: 2px;"
        )
        popup.setFixedSize(
            self._PREVIEW_SIZE.width() + 6,
            self._PREVIEW_SIZE.height() + 6,
        )
        popup.setMovie(movie)
        popup.move(self.mapToGlobal(QPoint(self.width() + 8, 0)))
        self._preview_popup = popup
        self._preview_movie = movie
        movie.start()
        popup.show()

    def _hide_preview(self) -> None:
        if self._preview_movie is not None:
            self._preview_movie.stop()
        if self._preview_popup is not None:
            self._preview_popup.close()
            self._preview_popup.deleteLater()
        self._preview_movie = None
        self._preview_popup = None


class _TransitionPreviewButton(_EffectPreviewButton):
    """Transition card that can replace its SVG icon with a looping GIF."""

    _ICON_SIZE = QSize(40, 24)

    def __init__(
        self,
        effect_type: str,
        name: str,
        description: str,
        icon: QIcon,
        preview_path: Path | None,
        animated: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(effect_type, name, description, icon, parent)
        self._static_icon = icon
        self._preview_mode = "icon"
        self._preview_path = Path(preview_path) if preview_path else None
        self._preview_movie: QMovie | None = None
        self._preview_size = _DEFAULT_TRANSITION_PREVIEW_SIZE
        self.set_preview_mode("animated" if animated else "icon")

    def set_preview_size(self, size: str) -> None:
        """Apply one of the supported animated-card size presets."""
        self._preview_size = (
            size
            if size in _TRANSITION_PREVIEW_SIZE_OPTIONS
            else _DEFAULT_TRANSITION_PREVIEW_SIZE
        )
        if self._preview_mode == "animated":
            self._apply_animated_geometry()

    @property
    def preview_size(self) -> str:
        """Return the current animated-card size preset."""
        return self._preview_size

    def _apply_animated_geometry(self) -> None:
        width, height, card_height = _TRANSITION_PREVIEW_SIZE_OPTIONS[
            self._preview_size
        ]
        self.setIconSize(QSize(width, height))
        self.setFixedHeight(card_height)
        self.setMinimumWidth(width + 4)

    def set_preview_mode(self, mode: str) -> None:
        """Switch this card between the legacy icon and animated preview."""
        self._preview_mode = (
            "animated"
            if mode == "animated" and self._preview_path is not None
            else "icon"
        )
        tile_mode = self._preview_mode == "animated"
        self.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon
            if tile_mode
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.setIconSize(self._ICON_SIZE)
        self.setFixedHeight(31)
        self.setMinimumWidth(0)
        if tile_mode:
            self._apply_animated_geometry()
        self.setStyleSheet(
            f"QToolButton {{"
            f" background: {_EFFECT_BG}; border: 1px solid {_EFFECT_BORDER_SUBTLE};"
            f" border-radius: 1px; color: {_EFFECT_TEXT}; padding: "
            f"{'2px' if tile_mode else '1px 4px'};"
            f" font-size: {'10px' if tile_mode else '11px'}; font-weight: 400;"
            f" text-align: {'center' if tile_mode else 'left'}; }}"
            f"QToolButton:hover {{ background: {_EFFECT_BG};"
            f" border-color: {_EFFECT_BORDER}; color: {C.TEXT_SECONDARY}; }}"
            f"QToolButton:pressed {{ background: {_EFFECT_BG}; }}"
        )
        if tile_mode:
            # The movie is created only when the card enters the visible
            # playback set. Until then the SVG is a cheap placeholder.
            self.setIcon(self._static_icon)
        else:
            self.release_preview_movie()

    @property
    def has_preview(self) -> bool:
        """Whether this card has a generated GIF available for lazy loading."""
        return self._preview_path is not None

    @property
    def is_preview_playing(self) -> bool:
        """Whether the lazily-created movie is currently running."""
        return (
            self._preview_movie is not None
            and self._preview_movie.state() == QMovie.MovieState.Running
        )

    def set_preview_playing(self, playing: bool) -> None:
        """Start a visible preview or release it when it leaves the viewport."""
        if not playing or self._preview_mode != "animated":
            self.release_preview_movie()
            return

        movie = self._ensure_preview_movie()
        if movie is None:
            return
        if movie.state() == QMovie.MovieState.Paused:
            movie.setPaused(False)
        elif movie.state() != QMovie.MovieState.Running:
            movie.start()

    def _ensure_preview_movie(self) -> QMovie | None:
        """Create a cache-light QMovie for this card on first visibility."""
        if self._preview_movie is not None:
            return self._preview_movie
        if self._preview_path is None:
            return None
        movie = QMovie(str(self._preview_path), parent=self)
        movie.setCacheMode(QMovie.CacheMode.CacheNone)
        width, height, _ = _TRANSITION_PREVIEW_SIZE_OPTIONS[self._preview_size]
        movie.setScaledSize(QSize(width, height))
        if not movie.isValid():
            movie.deleteLater()
            return None
        movie.frameChanged.connect(self._on_preview_frame_changed)
        self._preview_movie = movie
        if movie.jumpToFrame(0):
            self.setIcon(QIcon(movie.currentPixmap()))
        return movie

    def release_preview_movie(self) -> None:
        """Stop and destroy the decoder to keep off-screen cards cheap."""
        movie = self._preview_movie
        self._preview_movie = None
        if movie is not None:
            movie.stop()
            movie.deleteLater()
        if self._preview_mode == "animated":
            self.setIcon(self._static_icon)

    def _on_preview_frame_changed(self, _frame_number: int) -> None:
        if self._preview_mode == "animated" and self._preview_movie is not None:
            self.setIcon(QIcon(self._preview_movie.currentPixmap()))

    def closeEvent(self, event) -> None:
        self.release_preview_movie()
        super().closeEvent(event)


class _ResponsiveEffectSection(QWidget):
    """A category header plus a responsive effect or thumbnail-card grid."""

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        tile_mode: bool = False,
        tile_card_width: int = 140,
        tile_card_height: int = 100,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {_EFFECT_BG};")
        self.title = title
        self._tile_mode = tile_mode
        self._tile_card_width = tile_card_width
        self._tile_card_height = tile_card_height
        self.column_count = 1
        self._cards: List[_EffectPreviewButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color: {_EFFECT_TEXT}; font-size: 11px; font-weight: 500; padding: 1px 2px;"
        )
        self.title_label.setFixedHeight(16)
        layout.addWidget(self.title_label)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(4)
        layout.addLayout(self.grid)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._host_tree: Optional[QTreeWidget] = None
        self._host_item: Optional[QTreeWidgetItem] = None

    def bind_to_tree(self, tree: QTreeWidget, item: QTreeWidgetItem) -> None:
        """Keep the backing tree row exactly as tall as the visible grid."""
        self._host_tree = tree
        self._host_item = item
        self._sync_host_row_height()

    def unbind_from_tree(self) -> None:
        """Detach from a tree item before the backing tree is rebuilt."""
        self._host_tree = None
        self._host_item = None

    def _sync_host_row_height(self) -> None:
        if self._host_tree is None or self._host_item is None:
            return
        # Use the compact grid calculation instead of QWidget.sizeHint(),
        # which can include the old one-column height while the tree is being
        # resized. That stale height was the source of the large blank gaps.
        # R23（第七轮审计）：宿主 item 可能已被 tree.clear() 销毁（视频/
        # 音频树重建未解绑时的延迟 resize）——目标守卫自解绑，不再把
        # wrapped C/C++ object deleted 抛进 Qt 槽。
        try:
            self._host_item.setSizeHint(0, QSize(0, self.height()))
            self._host_tree.updateGeometries()
        except RuntimeError:
            self.unbind_from_tree()

    def add_card(self, card: _EffectPreviewButton) -> None:
        self._cards.append(card)
        card.setParent(self)
        self._reflow()

    def _desired_columns(self) -> int:
        # Thumbnail cards use the same compact tile density as the Motion
        # Graphic library; legacy icon cards keep their original breakpoints.
        width = max(1, self.width() - 8)
        card_width = self._tile_card_width if self._tile_mode else 200
        max_columns = 4 if self._tile_mode else 3
        return max(1, min(max_columns, (width + 5) // card_width))

    def _reflow(self) -> None:
        desired = self._desired_columns()
        self.column_count = int(desired)
        for card in self._cards:
            self.grid.removeWidget(card)
        for column in range(3):
            self.grid.setColumnStretch(column, 1 if column < desired else 0)
        for index, card in enumerate(self._cards):
            self.grid.addWidget(card, index // desired, index % desired)
        rows = (len(self._cards) + desired - 1) // desired
        card_height = self._tile_card_height if self._tile_mode else 31
        self.setFixedHeight(
            29 + max(1, rows) * card_height + max(0, rows - 1) * 4
        )
        self.updateGeometry()
        self._sync_host_row_height()

    def resizeEvent(self, event) -> None:
        previous = self.column_count
        self._reflow()
        if previous != self.column_count:
            self.parentWidget().updateGeometry() if self.parentWidget() else None
        super().resizeEvent(event)


# MIME types for drag and drop
MIME_TYPE_EFFECT = "application/x-topos-effect"
MIME_TYPE_TRANSITION = "application/x-topos-transition"
# Audio transitions live in their own MIME so the timeline drop router can
# route them to audio tracks (the video transition drop handler hard-codes
# ordered_video_tracks). The payload is still a transition_type string
# (e.g. "audio:constant_power").
MIME_TYPE_AUDIO_TRANSITION = "application/x-topos-audio-transition"
# Generators (Adjustment Clip / Solid Color) are dragged to CREATE a clip,
# not attach an effect — so they use a distinct MIME that the timeline drop
# router recognises and routes to _handle_generator_drop instead of
# _handle_effect_drop.
MIME_TYPE_GENERATOR = "application/x-topos-generator"


class EditEffectsPanel(QWidget):
    """
    Effects panel for browsing and applying effects.

    Signals:
        effect_add_requested: User requested to add an effect to a clip
        transition_add_requested: User requested to add a transition
        title_template_selected: C1 user picked a title template (preset_id)

    P7 Completion:
        - Audio Effects tab with draggable effects
        - Favorites system with star button toggle
        - Favorites stored in ConfigManager
    """

    # Signals for effect application
    effect_add_requested = pyqtSignal(str, str)  # effect_type, clip_id
    # P1-06: transition payload ("cross_dissolve" or "wipe:<direction>") and
    # the selected clip ID ("" when nothing is selected — receiver falls back
    # to the playhead). Drop-point resolution lives in TimelineView, which
    # owns the timeline and the shared add_transition_* command path.
    transition_apply_requested = pyqtSignal(str, str)
    # C1: title template double-click/apply (preset_id) — main_window
    # forwards this to EditController._on_title_template_selected for
    # placement on the timeline.
    title_template_selected = pyqtSignal(str)
    # Subtitle effects always target an existing subtitle cue.  The parameter
    # dict contains only user overrides; template defaults fill the rest.
    caption_effect_apply_requested = pyqtSignal(str, str, dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Prevent panel from expanding horizontally beyond dock size
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(280)

        # Effect registry
        self._registry = get_effect_registry()
        self._motion_graphic_registry = get_motion_graphic_generator_registry()
        self._motion_graphic_registry_unsubscribe = None
        # AUD-19802: cross-thread registry callbacks are coalesced onto the
        # UI thread through one queued invocation.
        self._motion_graphic_refresh_pending = False
        # AUD-19803: search rebuilds are debounced — the timer fires the
        # actual tree rebuild once typing pauses.
        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(200)
        self._search_debounce_timer.timeout.connect(self._apply_search_filter)

        # Config manager for favorites persistence
        self._config_manager = ConfigManager()

        # Favorites set (effect_types that are favorites)
        self._favorites: Set[str] = self._load_favorites()

        # Current selected clip (for double-click apply)
        self._selected_clip_id: Optional[str] = None
        self._selected_subtitle_clip_id: Optional[str] = None
        self._search_visible = False
        # AUD-19804: 防递归状态锁 —— 程序化更新控件（search_input.clear() 等）
        # 期间抑制 textChanged 触发的全量树重建。
        self._updating = False
        self._transition_preview_mode = self._load_transition_preview_mode()
        self._transition_preview_size = self._load_transition_preview_size()
        self._transition_preview_cards: List[_TransitionPreviewButton] = []
        self._transition_preview_sync_scheduled = False

        self._setup_ui()
        self._connect_signals()
        self._motion_graphic_registry_unsubscribe = self._motion_graphic_registry.subscribe(
            self._refresh_motion_graphic_generators
        )
        logger.debug("EditEffectsPanel initialized with Audio and Favorites support")

    # ==================== Favorites Management ====================

    def _load_favorites(self) -> Set[str]:
        """
        Load favorites from ConfigManager.

        Returns:
            Set of effect_type strings that are favorites.
        """
        favorites_list = self._config_manager.get("effect_favorites")
        if favorites_list is None:
            return set()
        return set(favorites_list)

    def _save_favorites(self) -> None:
        """Save favorites to ConfigManager."""
        self._config_manager.set("effect_favorites", list(self._favorites))

    def _load_transition_preview_mode(self) -> str:
        """Load the transition library presentation mode from preferences."""
        mode = self._config_manager.get("transition_preview_mode") or "icon"
        if isinstance(mode, str) and mode in {"icon", "animated"}:
            return mode
        return "icon"

    def _set_transition_preview_mode(self, mode: str) -> None:
        """Persist and apply a transition library presentation mode."""
        self._transition_preview_mode = mode if mode == "animated" else "icon"
        self._config_manager.set(
            "transition_preview_mode", self._transition_preview_mode
        )
        # Rebuild the transition sections so the tile geometry changes along
        # with the icon/movie. Static mode keeps the existing compact layout;
        # animated mode uses the larger thumbnail tiles used by the MG library.
        self._populate_transitions(self.transitions_tree)

    def _load_transition_preview_size(self) -> str:
        """Load the animated-card size preset from user preferences."""
        size = self._config_manager.get("transition_preview_size")
        if isinstance(size, str) and size in _TRANSITION_PREVIEW_SIZE_OPTIONS:
            return size
        return _DEFAULT_TRANSITION_PREVIEW_SIZE

    def _set_transition_preview_size(self, size: str) -> None:
        """Persist and apply an animated-card size preset."""
        if size not in _TRANSITION_PREVIEW_SIZE_OPTIONS:
            return
        self._transition_preview_size = size
        self._config_manager.set("transition_preview_size", size)
        self._populate_transitions(self.transitions_tree)

    def _transition_preview_geometry(self) -> tuple[int, int, int]:
        """Return GIF width, GIF height and card height for the current size."""
        return _TRANSITION_PREVIEW_SIZE_OPTIONS[self._transition_preview_size]

    @staticmethod
    def _transition_preview_path(stem: str) -> Path | None:
        """Resolve a generated GIF for a library card, if one exists."""
        path = _TRANSITION_PREVIEW_DIR / f"{stem}.gif"
        return path if path.is_file() else None

    def _build_library_context_menu_for_type(
        self,
        item_type: str,
        *,
        is_transition: bool = False,
        preview_stem: str = "",
    ) -> QMenu:
        """Build one context menu shared by cards, trees and the dock title."""
        menu = QMenu(self)
        favorite_action = None
        if item_type and not is_transition:
            is_favorite = self._is_favorite(item_type)
            favorite_action = menu.addAction(
                icon_favorite() if is_favorite else icon_favorite_outline(),
                I18N.tr(
                    "edit.effects.remove_favorite" if is_favorite
                    else "edit.effects.add_favorite",
                    "Remove from Favorites" if is_favorite
                    else "Add to Favorites",
                ),
            )

        show_preview_controls = is_transition or not item_type
        preview_actions = None
        if show_preview_controls:
            has_preview = (
                self._transition_preview_path(preview_stem) is not None
                if preview_stem
                else any(card.has_preview for card in self._transition_preview_cards)
            )
            icon_action = menu.addAction(
                I18N.tr("edit.effects.transition_icon_preview", "Icon Preview")
            )
            icon_action.setCheckable(True)
            icon_action.setChecked(self._transition_preview_mode == "icon")
            animated_action = menu.addAction(
                I18N.tr(
                    "edit.effects.transition_animated_preview",
                    "Animated Preview",
                )
            )
            animated_action.setCheckable(True)
            animated_action.setChecked(self._transition_preview_mode == "animated")
            animated_action.setEnabled(has_preview)
            if not has_preview:
                animated_action.setToolTip(
                    I18N.tr(
                        "edit.effects.transition_preview_unavailable",
                        "No generated GIF preview is available for this item",
                    )
                )
            preview_actions = (icon_action, animated_action)
            size_menu = menu.addMenu(
                I18N.tr("edit.effects.transition_preview_size", "GIF Preview Size")
            )
            size_menu.setEnabled(has_preview)
            for size, label in (
                ("small", "Small"),
                ("medium", "Medium"),
                ("large", "Large"),
            ):
                size_action = size_menu.addAction(
                    I18N.tr(f"edit.effects.transition_preview_{size}", label)
                )
                size_action.setCheckable(True)
                size_action.setChecked(self._transition_preview_size == size)
                size_action.setData(size)

        dock = find_parent_dock(self)
        if dock is not None:
            if menu.actions():
                menu.addSeparator()
            add_dock_management_actions(menu, dock)

        # Keep Python-side references on the menu so QAction data stays
        # private to this menu and the action handling remains straightforward.
        menu._preview_actions = preview_actions  # type: ignore[attr-defined]
        menu._favorite_action = favorite_action  # type: ignore[attr-defined]
        return menu

    def _show_library_context_menu_for_type(
        self,
        item_type: str,
        global_pos: QPoint,
        *,
        is_transition: bool = False,
        preview_stem: str = "",
    ) -> None:
        """Show the unified menu and apply the selected library action."""
        menu = self._build_library_context_menu_for_type(
            item_type,
            is_transition=is_transition,
            preview_stem=preview_stem,
        )
        action = menu.exec(global_pos)
        preview_actions = getattr(menu, "_preview_actions", None)
        if preview_actions and action == preview_actions[0]:
            self._set_transition_preview_mode("icon")
        elif preview_actions and action == preview_actions[1]:
            self._set_transition_preview_mode("animated")
        elif action is not None and action.data() in _TRANSITION_PREVIEW_SIZE_OPTIONS:
            self._set_transition_preview_size(str(action.data()))
        elif action == getattr(menu, "_favorite_action", None) and item_type:
            self._toggle_favorite(item_type)

    def show_unified_context_menu(self, global_pos: QPoint) -> None:
        """Show the same menu for blank space and the Effects dock title."""
        self._show_library_context_menu_for_type(
            "",
            global_pos,
            is_transition=self.tabs.currentIndex() == _TAB_INDEX_TRANSITIONS,
        )

    def _show_transition_context_menu_for_type(
        self, payload: str, global_pos: QPoint
    ) -> None:
        """Show the unified library context menu for a transition item."""
        if not payload:
            return
        if payload == "wipe:left":
            preview_stem = "wipe_left"
        elif payload == "wipe:up":
            preview_stem = "wipe_up"
        else:
            preview_stem = payload
        self._show_library_context_menu_for_type(
            payload,
            global_pos,
            is_transition=True,
            preview_stem=preview_stem,
        )

    def _is_favorite(self, effect_type: str) -> bool:
        """Check if an effect is favorited."""
        return effect_type in self._favorites

    def _toggle_favorite(self, effect_type: str) -> None:
        """
        Toggle favorite status for an effect.

        Args:
            effect_type: The effect type to toggle.
        """
        if effect_type in self._favorites:
            self._favorites.remove(effect_type)
            logger.debug(f"Removed favorite: {effect_type}")
        else:
            self._favorites.add(effect_type)
            logger.debug(f"Added favorite: {effect_type}")

        # Save to config
        self._save_favorites()

        # Refresh all trees to update star icons
        self._refresh_all_trees()

    def _refresh_all_trees(self) -> None:
        """Refresh all effect trees to update favorite icons."""
        self._populate_video_effects(self.video_effects_tree)
        self._populate_audio_effects(self.audio_effects_tree)
        self._populate_favorites(self.favorites_tree)

    def _show_effect_context_menu(self, pos: QPoint) -> None:
        """
        Show context menu for effect items.

        P7 Completion: Right-click menu to toggle favorite status.

        Args:
            pos: Position where context menu was requested.
        """
        # Get the tree that triggered this
        sender_tree = self.sender()
        if sender_tree is None:
            return

        # Get item at position
        item = sender_tree.itemAt(pos)
        if item is None:
            self.show_unified_context_menu(sender_tree.viewport().mapToGlobal(pos))
            return

        item_type = item.data(0, Qt.ItemDataRole.UserRole)
        if item_type != "effect":
            self.show_unified_context_menu(sender_tree.viewport().mapToGlobal(pos))
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if effect_type is None:
            self.show_unified_context_menu(sender_tree.viewport().mapToGlobal(pos))
            return

        self._show_effect_context_menu_for_type(effect_type, sender_tree.viewport().mapToGlobal(pos))

    def _show_transition_tree_context_menu(self, pos: QPoint) -> None:
        """Show the transition display-mode menu for a backing tree item."""
        item = self.transitions_tree.itemAt(pos)
        if item is None:
            self.show_unified_context_menu(self.transitions_tree.viewport().mapToGlobal(pos))
            return
        if item.data(0, Qt.ItemDataRole.UserRole) != "transition":
            self.show_unified_context_menu(self.transitions_tree.viewport().mapToGlobal(pos))
            return
        transition_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        direction = item.data(0, Qt.ItemDataRole.UserRole + 2)
        payload = (
            f"wipe:{direction}"
            if transition_type == "wipe" and direction
            else transition_type
        )
        self._show_transition_context_menu_for_type(
            str(payload or ""), self.transitions_tree.viewport().mapToGlobal(pos)
        )

    def _show_effect_context_menu_for_type(self, effect_type: str, global_pos: QPoint) -> None:
        """Show the unified menu for either a tree item or a visual card."""
        self._show_library_context_menu_for_type(effect_type, global_pos)

    def _setup_ui(self) -> None:
        """Set up the panel UI structure."""
        self.setObjectName("EditEffectsPanel")
        self.setStyleSheet(
            f"QWidget#EditEffectsPanel {{ background-color: {_EFFECT_BG}; }}"
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.show_unified_context_menu(self.mapToGlobal(pos))
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Search is opt-in from the small button at the right side of the tab bar.
        self.search_container = QWidget()
        self.search_container.setFixedHeight(28)
        search_bar = QHBoxLayout(self.search_container)
        search_bar.setContentsMargins(0, 0, 0, 0)
        search_bar.setSpacing(4)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            I18N.tr("edit.effects.search_placeholder", "Search effects...")
        )
        self.search_input.setFixedHeight(28)
        self.search_input.setStyleSheet(
            f"QLineEdit {{ background: {_EFFECT_BG}; border: 1px solid {_EFFECT_BORDER};"
            f" border-radius: 2px; color: {_EFFECT_TEXT}; padding: 3px 7px;"
            " font-size: 11px; }"
            f"QLineEdit:focus {{ border-color: {_EFFECT_BORDER}; }}"
        )
        search_bar.addWidget(self.search_input)
        self.search_container.setVisible(False)
        layout.addWidget(self.search_container)

        # Tab widget for categories
        self.tabs = QTabWidget()
        tab_bar = self.tabs.tabBar()
        tab_bar.setExpanding(True)
        tab_bar.setUsesScrollButtons(False)
        tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        tab_bar.setMinimumWidth(250)
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {_EFFECT_BG};
            }}
            QTabBar {{
                background-color: {_EFFECT_BG};
                border-bottom: 1px solid {C.BORDER_DARK};
            }}
            QTabBar::tab {{
                background: transparent;
                color: {_EFFECT_TEXT};
                border: 1px solid transparent;
                padding: 4px 12px;
                margin: 0px 1px 0px 0px;
                font-size: 11px;
                font-weight: 500;
                min-height: 20px;
            }}
            QTabBar::tab:hover {{
                background-color: {C.BG_HOVER};
                color: {C.TEXT_SECONDARY};
            }}
            QTabBar::tab:selected {{
                background-color: {C.BG_HOVER};
                color: {C.TEXT_PRIMARY};
                border-bottom: 2px solid {C.BORDER_MID};
            }}
        """)
        self.search_toggle_button = QToolButton()
        self.search_toggle_button.setIcon(icon_search(IconColors.DEFAULT))
        self.search_toggle_button.setIconSize(QSize(14, 14))
        self.search_toggle_button.setToolTip(I18N.tr("edit.effects.search_tooltip", "Search effects"))
        self.search_toggle_button.setAutoRaise(True)
        self.search_toggle_button.setStyleSheet(
            f"QToolButton {{ background: transparent; border: none;"
            f" padding: 3px; color: {_EFFECT_TEXT}; }}"
            f"QToolButton:hover {{ background: {_EFFECT_BG}; border: none; }}"
        )
        self.tabs.setCornerWidget(
            self.search_toggle_button,
            Qt.Corner.TopRightCorner,
        )

        # Video Effects tab
        video_effects_tab = QWidget()
        video_effects_tab.setStyleSheet(f"background-color: {_EFFECT_BG};")
        video_layout = QVBoxLayout(video_effects_tab)
        video_layout.setContentsMargins(4, 4, 4, 4)

        self.video_effects_tree = QTreeWidget()
        self.video_effects_tree.setHeaderHidden(True)
        self.video_effects_tree.setStyleSheet(_EFFECT_TREE_STYLE)
        self.video_effects_tree.setIconSize(QSize(48, 28))
        self.video_effects_tree.setColumnCount(1)
        self.video_effects_tree.setIndentation(0)
        self.video_effects_tree.header().setStretchLastSection(True)
        self.video_effects_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.video_effects_tree.setDragEnabled(True)
        self.video_effects_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.video_effects_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_effects_tree.customContextMenuRequested.connect(self._show_effect_context_menu)
        video_layout.addWidget(self.video_effects_tree)

        # Motion Graphic generators are video generators, not a separate
        # effect kind.  Their cards therefore live as the final section of
        # the Video palette.  Keep this alias for integrations that used the
        # original attribute; it now points at the Video tree.
        self.motion_graphic_generators_tree = self.video_effects_tree
        self._populate_video_effects(self.video_effects_tree)

        self.tabs.addTab(
            video_effects_tab,
            I18N.tr("edit.effects.video_effects", "Video")
        )

        # Transitions tab
        transitions_tab = QWidget()
        transitions_tab.setStyleSheet(f"background-color: {_EFFECT_BG};")
        trans_layout = QVBoxLayout(transitions_tab)
        trans_layout.setContentsMargins(4, 4, 4, 4)

        self.transitions_tree = QTreeWidget()
        self.transitions_tree.setHeaderHidden(True)
        self.transitions_tree.setStyleSheet(_EFFECT_TREE_STYLE)
        self.transitions_tree.setIconSize(QSize(48, 28))
        self.transitions_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.transitions_tree.setDragEnabled(True)
        self.transitions_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.transitions_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.transitions_tree.customContextMenuRequested.connect(
            self._show_transition_tree_context_menu
        )
        self._populate_transitions(self.transitions_tree)
        trans_layout.addWidget(self.transitions_tree)

        self.tabs.addTab(
            transitions_tab,
            I18N.tr("edit.effects.transitions", "Transitions")
        )

        # Titles tab: title templates plus reusable animated caption effects.
        titles_tab = QWidget()
        titles_tab.setStyleSheet(f"background-color: {_EFFECT_BG};")
        titles_layout = QVBoxLayout(titles_tab)
        titles_layout.setContentsMargins(4, 4, 4, 4)

        self.titles_tree = QTreeWidget()
        self.titles_tree.setHeaderHidden(True)
        self.titles_tree.setStyleSheet(_EFFECT_TREE_STYLE)
        self.titles_tree.setIconSize(QSize(48, 28))
        self.titles_tree.setColumnCount(1)
        self.titles_tree.setIndentation(0)
        self.titles_tree.header().setStretchLastSection(True)
        self._populate_titles(self.titles_tree)
        self.titles_tree.itemDoubleClicked.connect(self._on_title_item_double_clicked)
        # F2.9：用户模板管理（重命名/删除）——右键菜单 + 操作后刷新树
        self.titles_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.titles_tree.customContextMenuRequested.connect(
            self._on_title_template_context_menu)
        titles_layout.addWidget(self.titles_tree)

        caption_effect_controls = QWidget()
        caption_effect_controls.setStyleSheet(
            f"background: {_EFFECT_BG}; border-top: 1px solid {_EFFECT_BORDER_SUBTLE};"
        )
        controls_layout = QFormLayout(caption_effect_controls)
        controls_layout.setContentsMargins(8, 6, 8, 4)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(3)

        self.caption_effect_in_frames = QSpinBox(caption_effect_controls)
        self.caption_effect_in_frames.setRange(1, 60)
        self.caption_effect_in_frames.setValue(10)
        self.caption_effect_in_frames.setSuffix(I18N.tr("edit.frames_suffix", " f"))
        self.caption_effect_in_frames.setToolTip(
            I18N.tr(
                "edit.effects.caption_in_frames_tooltip",
                "Frames used by the selected subtitle effect to enter.")
        )
        controls_layout.addRow(
            I18N.tr("edit.effects.caption_in_frames", "Enter Duration"),
            self.caption_effect_in_frames,
        )

        self.caption_effect_out_frames = QSpinBox(caption_effect_controls)
        self.caption_effect_out_frames.setRange(0, 60)
        self.caption_effect_out_frames.setValue(8)
        self.caption_effect_out_frames.setSuffix(I18N.tr("edit.frames_suffix", " f"))
        self.caption_effect_out_frames.setToolTip(
            I18N.tr(
                "edit.effects.caption_out_frames_tooltip",
                "Frames used by the selected subtitle effect to exit; 0 holds to the end.")
        )
        controls_layout.addRow(
            I18N.tr("edit.effects.caption_out_frames", "Exit Duration"),
            self.caption_effect_out_frames,
        )

        self.caption_effect_distance = QSpinBox(caption_effect_controls)
        self.caption_effect_distance.setRange(10, 250)
        self.caption_effect_distance.setValue(100)
        self.caption_effect_distance.setSuffix(" %")
        self.caption_effect_distance.setToolTip(
            I18N.tr(
                "edit.effects.caption_distance_tooltip",
                "Travel distance for slide effects, measured relative to the subtitle size.")
        )
        controls_layout.addRow(
            I18N.tr("edit.effects.caption_distance", "Travel Distance"),
            self.caption_effect_distance,
        )
        titles_layout.addWidget(caption_effect_controls)

        titles_hint = QLabel(I18N.tr(
            "edit.effects.titles_hint",
            "Double-click a title template to create a subtitle at the "
            "playhead; with a subtitle selected, double-click a caption "
            "effect to apply its animation.",
        ))
        titles_hint.setWordWrap(True)
        titles_hint.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        titles_layout.addWidget(titles_hint)

        self.tabs.addTab(
            titles_tab,
            I18N.tr("edit.effects.titles", "Titles")
        )

        # Audio Effects tab (P7 Completion - full implementation)
        audio_effects_tab = QWidget()
        audio_effects_tab.setStyleSheet(f"background-color: {_EFFECT_BG};")
        audio_layout = QVBoxLayout(audio_effects_tab)
        audio_layout.setContentsMargins(4, 4, 4, 4)

        self.audio_effects_tree = QTreeWidget()
        self.audio_effects_tree.setHeaderHidden(True)
        self.audio_effects_tree.setStyleSheet(_EFFECT_TREE_STYLE)
        self.audio_effects_tree.setIconSize(QSize(48, 28))
        self.audio_effects_tree.setColumnCount(1)
        self.audio_effects_tree.setIndentation(0)
        self.audio_effects_tree.header().setStretchLastSection(True)
        self.audio_effects_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.audio_effects_tree.setDragEnabled(True)
        self.audio_effects_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.audio_effects_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.audio_effects_tree.customContextMenuRequested.connect(self._show_effect_context_menu)
        self._populate_audio_effects(self.audio_effects_tree)
        audio_layout.addWidget(self.audio_effects_tree)

        self.tabs.addTab(
            audio_effects_tab,
            I18N.tr("edit.effects.audio_effects", "Audio")
        )

        layout.addWidget(self.tabs, stretch=1)

        # Favorites tab (P7 Completion - replaces placeholder section)
        favorites_tab = QWidget()
        favorites_tab.setStyleSheet(f"background-color: {_EFFECT_BG};")
        fav_layout = QVBoxLayout(favorites_tab)
        fav_layout.setContentsMargins(4, 4, 4, 4)

        self.favorites_tree = QTreeWidget()
        self.favorites_tree.setHeaderHidden(True)
        self.favorites_tree.setStyleSheet(_EFFECT_TREE_STYLE)
        self.favorites_tree.setIconSize(QSize(48, 28))
        self.favorites_tree.setColumnCount(1)
        self.favorites_tree.setIndentation(0)
        self.favorites_tree.header().setStretchLastSection(True)
        self.favorites_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.favorites_tree.setDragEnabled(True)
        self.favorites_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.favorites_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.favorites_tree.customContextMenuRequested.connect(self._show_effect_context_menu)
        self._populate_favorites(self.favorites_tree)
        fav_layout.addWidget(self.favorites_tree)

        self.tabs.addTab(
            favorites_tab,
            I18N.tr("edit.effects.favorites", "Favorites")
        )
        # Kept as a non-visual state sink for existing empty-selection messages.
        self.info_label = QLabel()
        self.info_label.setVisible(False)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        # Search filter
        self.search_input.textChanged.connect(self._on_search_changed)
        self.search_toggle_button.clicked.connect(self._toggle_search_visibility)

        # Double-click to apply effect
        self.video_effects_tree.itemDoubleClicked.connect(self._on_effect_double_clicked)
        self.audio_effects_tree.itemDoubleClicked.connect(self._on_effect_double_clicked)
        self.favorites_tree.itemDoubleClicked.connect(self._on_effect_double_clicked)

        # Transitions double-click (placeholder)
        self.transitions_tree.itemDoubleClicked.connect(self._on_transition_double_clicked)
        self.tabs.currentChanged.connect(self._on_effect_tab_changed)
        self.transitions_tree.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_transition_preview_sync()
        )
        self.transitions_tree.horizontalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_transition_preview_sync()
        )
        self.transitions_tree.viewport().installEventFilter(self)

        # Inject custom startDrag onto each tree so drags carry the
        # application MIME types (MIME_TYPE_EFFECT / MIME_TYPE_TRANSITION)
        # the timeline's dragEnterEvent routes on. Without this the trees
        # fall back to QTreeWidget's default startDrag, which emits a
        # generic model-data MIME the timeline can't recognise — drags
        # then get misrouted into the external-file-import branch and fail
        # with "no project set".
        _inject_drag_support(self)
        _inject_transition_drag_support(self)
        _inject_audio_drag_support(self)
        _inject_favorites_drag_support(self)
        self.video_effects_tree.itemDoubleClicked.connect(
            self._on_motion_graphic_generator_double_clicked
        )

    def closeEvent(self, event) -> None:
        """Release the global generator-palette listener with the panel."""
        self._stop_transition_preview_playback()
        unsubscribe = self._motion_graphic_registry_unsubscribe
        self._motion_graphic_registry_unsubscribe = None
        if callable(unsubscribe):
            unsubscribe()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        """Resume only the transition previews currently inside the viewport."""
        super().showEvent(event)
        self._schedule_transition_preview_sync()

    def hideEvent(self, event) -> None:
        """Release all GIF decoders while the effects panel is hidden."""
        self._stop_transition_preview_playback()
        super().hideEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.transitions_tree.viewport()
            and event.type() in {
                QEvent.Type.Resize,
                QEvent.Type.Show,
                QEvent.Type.Hide,
            }
        ):
            self._schedule_transition_preview_sync()
        return super().eventFilter(watched, event)

    def _on_effect_tab_changed(self, _index: int) -> None:
        """Stop previews when the Transitions tab is not the active page."""
        self._schedule_transition_preview_sync()
        # F2.9：切到 Titles 页时重列模板——保存/删除/重命名后无需重启面板
        if self.titles_tree is not None and self.titles_tree.isVisible():
            self._populate_titles(self.titles_tree)

    def _on_title_template_context_menu(self, pos: Any) -> None:
        """F2.9：My Templates 条目的重命名/删除（内置模板不可改）。"""
        from PyQt6.QtWidgets import QInputDialog, QMenu

        from src.features.edit.core.title_templates import (
            delete_title_template,
            list_user_title_templates,
            rename_title_template,
        )

        item = self.titles_tree.itemAt(pos)
        if item is None or item.parent() is None:
            return
        preset_id = item.data(0, _TITLE_PRESET_ROLE)
        preset = next(
            (p for p in list_user_title_templates()
             if p.preset_id == preset_id), None)
        if preset is None:
            return  # 内置模板不可管理（仅用户模板落盘可改）
        menu = QMenu(self.titles_tree)
        rename_action = menu.addAction(
            I18N.tr("edit.effects.rename_template", "重命名…"))
        delete_action = menu.addAction(
            I18N.tr("edit.effects.delete_template", "删除"))
        selected = menu.exec(self.titles_tree.viewport().mapToGlobal(pos))
        if selected == rename_action:
            name, ok = QInputDialog.getText(
                self, I18N.tr("edit.effects.rename_template", "重命名…"),
                I18N.tr("timeline.subtitle.template_name", "Template name:"),
                text=preset.label)
            if ok and name.strip():
                try:
                    rename_title_template(preset.preset_id, name.strip())
                except ValueError as exc:
                    logger.warning("Rename template failed: %s", exc)
                    return
                self._populate_titles(self.titles_tree)
        elif selected == delete_action:
            delete_title_template(preset.preset_id)
            self._populate_titles(self.titles_tree)

    def _schedule_transition_preview_sync(self) -> None:
        """Coalesce layout/scroll events into one viewport reconciliation."""
        if self._transition_preview_sync_scheduled:
            return
        self._transition_preview_sync_scheduled = True
        QTimer.singleShot(0, self._run_scheduled_transition_preview_sync)

    def _run_scheduled_transition_preview_sync(self) -> None:
        self._transition_preview_sync_scheduled = False
        self._sync_transition_preview_playback()

    def _stop_transition_preview_playback(self) -> None:
        """Stop and release every transition GIF decoder."""
        for card in self._transition_preview_cards:
            card.set_preview_playing(False)

    def _sync_transition_preview_playback(self) -> None:
        """Play only a bounded set of cards intersecting the scroll viewport."""
        if (
            self._transition_preview_mode != "animated"
            or not self.isVisible()
            or not self.transitions_tree.isVisible()
        ):
            self._stop_transition_preview_playback()
            return

        viewport = self.transitions_tree.viewport()
        viewport_rect = viewport.rect()
        visible: list[tuple[int, int, _TransitionPreviewButton]] = []
        for card in self._transition_preview_cards:
            if not card.has_preview or not card.isVisible():
                continue
            top_left = card.mapTo(viewport, QPoint(0, 0))
            card_rect = QRect(top_left, card.size())
            if viewport_rect.intersects(card_rect):
                visible.append((card_rect.top(), card_rect.left(), card))

        visible.sort(key=lambda item: (item[0], item[1]))
        active_ids = {
            id(card)
            for _, _, card in visible[:_MAX_ACTIVE_TRANSITION_PREVIEWS]
        }
        for card in self._transition_preview_cards:
            card.set_preview_playing(id(card) in active_ids)

    def _toggle_search_visibility(self) -> None:
        """Toggle the optional search field from the tab-bar search icon."""
        self._search_visible = not self._search_visible
        self.search_container.setVisible(self._search_visible)
        if self._search_visible:
            self.search_input.setFocus()
            self.search_toggle_button.setToolTip(
                I18N.tr("edit.effects.hide_search_tooltip", "Hide effect search"))
        else:
            # AUD-19804: 程序化清空须持有 _updating 状态锁，抑制 textChanged
            # 重入导致的 _populate_video_effects 额外全量重建。
            self._updating = True
            try:
                self.search_input.clear()
            finally:
                self._updating = False
            self._search_debounce_timer.stop()
            self.search_toggle_button.setToolTip(
                I18N.tr("edit.effects.search_tooltip", "Search effects"))

    def _start_effect_drag_for_type(self, effect_type: str) -> None:
        """Start the same timeline-compatible drag used by the legacy trees."""
        mime_key = (
            MIME_TYPE_GENERATOR
            if effect_type in GENERATOR_EFFECT_TYPES
            else MIME_TYPE_EFFECT
        )
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setData(mime_key, QByteArray(effect_type.encode("utf-8")))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)
        logger.debug("Started drag for effect card: %s", effect_type)

    def _on_effect_card_double_clicked(self, effect_type: str) -> None:
        """Apply an effect card using the same rules as tree double-clicks."""
        if effect_type in GENERATOR_EFFECT_TYPES:
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.generator_drag_hint",
                    "Drag generators onto the timeline to create a clip",
                )
            )
            return
        if effect_type and self._selected_clip_id:
            self.effect_add_requested.emit(effect_type, self._selected_clip_id)
            return
        self.info_label.setText(
            I18N.tr(
                "edit.effects.no_clip_selected",
                "Select a clip first, then double-click effect",
            )
        )

    def _build_effect_section(
        self,
        tree: QTreeWidget,
        category: str,
        effects: List[EffectDefinition],
    ) -> None:
        """Populate a category and its responsive visual card grid."""
        if not effects:
            return

        category_display = self._registry.get_category_display_name(category)
        category_item = QTreeWidgetItem(tree, [category_display])
        category_item.setData(0, Qt.ItemDataRole.UserRole, "category")
        category_item.setData(0, Qt.ItemDataRole.UserRole + 1, category)
        category_item.setFlags(category_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        # The category label is rendered by the visual section widget. Keeping
        # the backing item's text would draw a second, misaligned title.
        category_item.setText(0, "")

        section = _ResponsiveEffectSection(category_display, tree)
        for effect_def in effects:
            # T-P1-20: preview_only effects (e.g. Edit plugins) get an
            # explicit delivery restriction badge in the UI — same source
            # of truth as Deliver preflight / AI manifest
            # (resolve_execution_capability), rejection reason verbatim.
            restriction_code, restriction_message = "", ""
            try:
                from src.features.shared.effects import (
                    get_effect_catalog, resolve_execution_capability,
                )

                execution = resolve_execution_capability(
                    effect_def.effect_type,
                    catalog_known=get_effect_catalog().is_known(
                        effect_def.effect_type),
                )
                restriction_code, restriction_message = \
                    execution.delivery_restriction()
            except Exception:  # noqa: BLE001 — badge failure must not break listing
                restriction_code, restriction_message = "", ""
            suffix = (
                I18N.tr("edit.effects.preview_only_suffix", " (Preview Only)")
                if restriction_code else ""
            )
            effect_item = QTreeWidgetItem(
                category_item, [effect_def.name + suffix])
            effect_item.setData(0, Qt.ItemDataRole.UserRole, "effect")
            effect_item.setData(0, Qt.ItemDataRole.UserRole + 1, effect_def.effect_type)
            effect_item.setData(0, Qt.ItemDataRole.UserRole + 2, effect_def.description)
            tooltip = f"{effect_def.name}\n{effect_def.description}"
            if restriction_code:
                tooltip += f"\n⚠ {restriction_message}（{restriction_code}）"
            effect_item.setToolTip(0, tooltip)
            # AUD-19803: rasterize the effect's icon once and reuse it in
            # both places (served by _effect_preview_icon's lru_cache).
            icon = _effect_preview_icon(effect_def.effect_type)
            effect_item.setIcon(0, icon)
            # Keep source items for keyboard/API compatibility; cards are the
            # responsive presentation of the same definitions.
            tree.setRowHidden(
                category_item.indexOfChild(effect_item),
                tree.indexFromItem(category_item),
                True,
            )

            card = _EffectPreviewButton(
                effect_def.effect_type,
                effect_def.name,
                effect_def.description,
                icon,
                section,
            )
            if self._is_favorite(effect_def.effect_type):
                card.setToolTip(f"★ {effect_def.name}\n{effect_def.description}")
            card.double_clicked.connect(self._on_effect_card_double_clicked)
            card.drag_requested.connect(self._start_effect_drag_for_type)
            card.context_menu_requested.connect(self._show_effect_context_menu_for_type)
            section.add_card(card)

        tree.setItemWidget(category_item, 0, section)
        section.bind_to_tree(tree, category_item)
        category_item.setExpanded(True)

    def _unbind_responsive_sections(self, tree: QTreeWidget) -> None:
        """R23（第七轮审计）：树重建前解绑全部响应式 section 宿主引用。

        ``tree.clear()`` 会销毁 QTreeWidgetItem 的 C++ 对象，section 残留
        的 ``_host_item`` 在延迟 resize/reflow 时触发 wrapped C/C++ object
        deleted（重开工程后实测 9 次未处理 RuntimeError）。transition 树
        （_populate_transitions）已有该解绑，这里统一到所有承载
        _ResponsiveEffectSection 的树。
        """
        for index in range(tree.topLevelItemCount()):
            widget = tree.itemWidget(tree.topLevelItem(index), 0)
            if isinstance(widget, _ResponsiveEffectSection):
                widget.unbind_from_tree()

    def _populate_video_effects(self, tree: QTreeWidget) -> None:
        """
        Populate video effects tree from registry.

        Groups effects by category.
        P7 Completion: Shows favorite icon on favorited effects.
        """
        self._close_motion_graphic_hover_previews(tree)
        self._unbind_responsive_sections(tree)
        tree.clear()

        # Get all effects from registry
        categories = self._registry.get_categories()

        for category in categories:
            # Skip audio category (has its own tab)
            if category == "audio":
                continue
            effects = [
                effect_def
                for effect_def in self._registry.get_effects_by_category(category)
                if effect_def.effect_type not in NON_ATTACHABLE_VIDEO_EFFECT_TYPES
            ]
            self._build_effect_section(tree, category, effects)

        self._append_motion_graphic_generators(tree)
        tree.expandAll()

    def _append_motion_graphic_generators(
        self, tree: QTreeWidget, filter_text: str = "",
    ) -> None:
        """Append MG generators below regular Video effects in the same tree."""
        templates = self._motion_graphic_registry.list_templates()
        normalized_filter = filter_text.casefold().strip()
        if normalized_filter:
            templates = tuple(
                template for template in templates
                if normalized_filter in template_display_name(template).casefold()
                or normalized_filter in template_description(template).casefold()
            )
        if not templates:
            return

        mg_section_title = I18N.tr("edit.effects.mg_section", "MG Generators")
        category_item = QTreeWidgetItem(tree, [mg_section_title])
        category_item.setData(0, Qt.ItemDataRole.UserRole, "category")
        category_item.setFlags(category_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        category_item.setText(0, "")
        section = _ResponsiveEffectSection(mg_section_title, tree)
        for template in templates:
            template_item = QTreeWidgetItem(category_item, [template_display_name(template)])
            template_item.setData(0, Qt.ItemDataRole.UserRole, "template")
            template_item.setData(0, Qt.ItemDataRole.UserRole + 1, template.template_id)
            tree.setRowHidden(
                category_item.indexOfChild(template_item),
                tree.indexFromItem(category_item),
                True,
            )
            card = _MotionGraphicPreviewButton(
                f"motion_graphic_template:{template.template_id}",
                template_display_name(template),
                template_description(template) or "Reusable HTML/CSS/Canvas generator",
                self._motion_graphic_registry.preview_path(
                    template.template_id, template.version,
                ),
                section,
            )
            card.double_clicked.connect(
                self._on_motion_graphic_generator_double_clicked_by_id
            )
            card.drag_requested.connect(self._start_motion_graphic_generator_drag_by_id)
            section.add_card(card)
        tree.setItemWidget(category_item, 0, section)
        section.bind_to_tree(tree, category_item)
        category_item.setExpanded(True)

    @staticmethod
    def _close_motion_graphic_hover_previews(tree: QTreeWidget) -> None:
        """Close MG hover previews before ``tree.clear()`` (AUD-19801).

        Clearing the tree deletes the cards while the pointer can still be
        over one (e.g. typing in the search box); ``leaveEvent`` never
        reaches a deleted widget. Without this pass the parentless ToolTip
        window would keep showing and decoding its GIF until an arbitrary
        GC moment.
        """
        for card in tree.findChildren(_MotionGraphicPreviewButton):
            card._hide_preview()

    def _refresh_motion_graphic_generators(self) -> None:
        """Refresh the palette after an AI/user template is registered.

        AUD-19802: the registry dispatches listeners synchronously on the
        CALLER's thread (AI worker threads included, e.g.
        ``promote_html_template(publish_to_effects=True)``). Rebuilding
        QTreeWidget/QTreeWidgetItem off the UI thread is forbidden, so when
        invoked from a foreign thread the rebuild is marshalled through a
        queued invocation on the panel's thread; a pending flag coalesces
        bursts of registrations into one rebuild.
        """
        panel_thread = self.thread()
        if panel_thread is None or panel_thread is QThread.currentThread():
            self._refresh_motion_graphic_generators_in_ui_thread()
            return
        if self._motion_graphic_refresh_pending:
            return
        self._motion_graphic_refresh_pending = True
        QMetaObject.invokeMethod(
            self,
            "_refresh_motion_graphic_generators_in_ui_thread",
            Qt.ConnectionType.QueuedConnection,
        )

    @pyqtSlot()
    def _refresh_motion_graphic_generators_in_ui_thread(self) -> None:
        """Run the palette rebuild on the panel's (UI) thread."""
        self._motion_graphic_refresh_pending = False
        if hasattr(self, "video_effects_tree"):
            self._populate_video_effects(self.video_effects_tree)

    def _start_motion_graphic_generator_drag_by_id(self, template_key: str) -> None:
        template_id = str(template_key).split(":", 1)[-1]
        template = self._motion_graphic_registry.get(template_id)
        if template is None:
            return
        drag = QDrag(self)
        mime = QMimeData()
        payload = {
            "template_id": template_id,
            "params": self._motion_graphic_parameter_values(template),
        }
        mime.setData(
            MOTION_GRAPHIC_GENERATOR_MIME,
            QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        )
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _start_motion_graphic_generator_drag(self, supported_actions: Qt.DropAction) -> None:
        del supported_actions
        item = self.motion_graphic_generators_tree.currentItem()
        if item is None or item.data(0, Qt.ItemDataRole.UserRole) != "template":
            return
        self._start_motion_graphic_generator_drag_by_id(
            str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
        )

    def _on_motion_graphic_generator_double_clicked_by_id(self, template_key: str) -> None:
        template_id = str(template_key).split(":", 1)[-1]
        template = self._motion_graphic_registry.get(template_id)
        if template is None:
            return
        self.info_label.setText(
            I18N.tr(
                "edit.effects.mg_section_hint",
                "{name}: drag to the timeline to create a generator clip; "
                "adjust it in the MG editor",
                name=template_display_name(template),
            )
        )

    def _motion_graphic_parameter_values(self, template: Any) -> dict[str, Any]:
        """Create clips with template defaults; changes belong in the MG editor."""
        return dict(template.default_params())

    def _on_motion_graphic_generator_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        if item.data(0, Qt.ItemDataRole.UserRole) == "template":
            self._on_motion_graphic_generator_double_clicked_by_id(
                str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            )

    def _populate_audio_effects(self, tree: QTreeWidget) -> None:
        """
        Populate audio effects tree from registry.

        P7 Completion - Audio Effects tab implementation.
        Groups effects by category (all audio effects under "Audio" category).
        Shows favorite icon on favorited effects.

        P1-07: an empty registry no longer renders a generic "coming soon"
        placeholder — that read as a missing product capability. Instead the
        builtin set is health-checked (registry definition + audio_dsp
        backend) and each missing effect is listed with the concrete reason.
        """
        self._unbind_responsive_sections(tree)
        tree.clear()

        # Get audio effects from registry
        audio_effects = self._registry.get_effects_by_category("audio")

        if not audio_effects:
            self._show_audio_effect_diagnostics(tree)
            return

        self._build_effect_section(tree, "audio", audio_effects)

        tree.expandAll()

    def _show_audio_effect_diagnostics(self, tree: QTreeWidget) -> None:
        """Render per-effect diagnostics for an empty audio registry (P1-07)."""
        from src.features.edit.core.effect_registry import (
            get_effect_registry_health,
        )

        health = get_effect_registry_health(self._registry)

        header_text = I18N.tr(
            "edit.effects.audio_registry_problem",
            "Audio effects failed to load — details below",
        )
        if health.is_healthy:
            # Registry probes are healthy yet nothing is registered under
            # the "audio" category: a filtered/derived view issue, not a
            # missing capability.
            header_text = I18N.tr(
                "edit.effects.audio_registry_filtered",
                "Audio effects are hidden by the current filter",
            )
        header = QTreeWidgetItem(tree, [header_text])
        header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        if health.registry_error:
            error_item = QTreeWidgetItem(header, [health.registry_error])
            error_item.setFlags(error_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        for status in health.missing:
            row = QTreeWidgetItem(
                header, [f"{status.effect_type}: {status.reason}"]
            )
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        tree.expandAll()

    def _populate_favorites(self, tree: QTreeWidget) -> None:
        """
        Populate favorites tree from saved favorites.

        P7 Completion - Favorites tab implementation.
        Shows all favorited effects from any category.
        """
        # R23 复查补齐：favorites 树同样经 _build_effect_section 承载
        # 响应式 section，clear 前必须解绑（星标增删触发的重建路径）。
        self._unbind_responsive_sections(tree)
        tree.clear()

        if not self._favorites:
            # Show placeholder when no favorites
            placeholder = QTreeWidgetItem(
                tree,
                [I18N.tr("edit.effects.no_favorites", "No favorites yet. Click the star icon to add.")]
            )
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            return

        # Group favorites by category
        favorites_by_category: Dict[str, List[EffectDefinition]] = {}

        for effect_type in self._favorites:
            effect_def = self._registry.get_definition(effect_type)
            if effect_def:
                category = effect_def.category
                if category not in favorites_by_category:
                    favorites_by_category[category] = []
                favorites_by_category[category].append(effect_def)

        # Build tree with responsive category grids
        for category, effects in favorites_by_category.items():
            self._build_effect_section(tree, category, effects)

        tree.expandAll()

    def _start_transition_drag_for_payload(self, payload: str) -> None:
        """Start a timeline-compatible drag from a visual transition card.

        Audio transitions (``audio:*`` payloads) ship under their own MIME so the
        timeline drop router can target audio tracks instead of video tracks.
        """
        mime_type = (
            MIME_TYPE_AUDIO_TRANSITION if is_audio_transition(payload)
            else MIME_TYPE_TRANSITION
        )
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setData(mime_type, QByteArray(payload.encode("utf-8")))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)
        logger.debug("Started drag for transition card (%s): %s", mime_type, payload)

    def _on_transition_card_double_clicked(self, payload: str) -> None:
        """Card double-click applies the transition like the legacy tree (P1-06)."""
        logger.debug(
            f"Transition card double-clicked: {payload} "
            f"(clip: {self._selected_clip_id or 'playhead fallback'})"
        )
        self.transition_apply_requested.emit(
            payload, self._selected_clip_id or ""
        )

    def _build_transition_section(
        self,
        tree: QTreeWidget,
        category: str,
        entries: List[tuple[str, str, str, Optional[str]]],
    ) -> None:
        """Build a responsive transition category using the shared card grid."""
        if not entries:
            return

        category_item = QTreeWidgetItem(tree, [""])
        category_item.setData(0, Qt.ItemDataRole.UserRole, "category")
        category_item.setData(0, Qt.ItemDataRole.UserRole + 1, category)
        category_item.setFlags(category_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

        tile_mode = self._transition_preview_mode == "animated" and any(
            self._transition_preview_path(
                icon_stem if transition_type == "wipe" else transition_type
            ) is not None
            for _, transition_type, icon_stem, _ in entries
        )
        preview_width, _, preview_card_height = self._transition_preview_geometry()
        section = _ResponsiveEffectSection(
            category,
            tree,
            tile_mode=tile_mode,
            tile_card_width=preview_width + 4,
            tile_card_height=preview_card_height,
        )
        for label, transition_type, icon_stem, direction in entries:
            item = QTreeWidgetItem(category_item, [label])
            item.setData(0, Qt.ItemDataRole.UserRole, "transition")
            item.setData(0, Qt.ItemDataRole.UserRole + 1, transition_type)
            if direction:
                item.setData(0, Qt.ItemDataRole.UserRole + 2, direction)
            item.setIcon(0, _transition_preview_icon(icon_stem))
            tree.setRowHidden(
                category_item.indexOfChild(item),
                tree.indexFromItem(category_item),
                True,
            )

            payload = f"wipe:{direction}" if direction else transition_type
            preview_stem = icon_stem if transition_type == "wipe" else transition_type
            card = _TransitionPreviewButton(
                payload,
                label,
                "Transition",
                _transition_preview_icon(icon_stem),
                self._transition_preview_path(preview_stem),
                self._transition_preview_mode == "animated",
                section,
            )
            card.set_preview_size(self._transition_preview_size)
            card.double_clicked.connect(self._on_transition_card_double_clicked)
            card.drag_requested.connect(self._start_transition_drag_for_payload)
            card.context_menu_requested.connect(
                self._show_transition_context_menu_for_type
            )
            self._transition_preview_cards.append(card)
            section.add_card(card)

        tree.setItemWidget(category_item, 0, section)
        section.bind_to_tree(tree, category_item)
        category_item.setExpanded(True)

    def _populate_transitions(self, tree: QTreeWidget) -> None:
        """Populate transitions in the same responsive grid as video/audio."""
        self._stop_transition_preview_playback()
        for index in range(tree.topLevelItemCount()):
            section = tree.itemWidget(tree.topLevelItem(index), 0)
            if isinstance(section, _ResponsiveEffectSection):
                section.unbind_from_tree()
        self._transition_preview_cards = []
        tree.clear()
        tree.setIconSize(QSize(40, 24))

        grouped: Dict[str, List[tuple[str, str, str, Optional[str]]]] = {}
        for definition in TRANSITION_DEFINITIONS:
            grouped.setdefault(definition.category, []).append(
                (definition.label, definition.transition_type, definition.transition_type, None)
            )

        # Keep the two useful Wipe presets from the original palette. The
        # direction is carried in MIME data and becomes the initial inspector
        # value after the drop.
        grouped.setdefault("Motion", []).extend(
            (
                ("Wipe From Left", "wipe", "wipe_left", "left"),
                ("Wipe From Top", "wipe", "wipe_up", "up"),
            )
        )

        # C2: user-defined transition group ("My Transitions"); type ids
        # are registered into the MIME routing table.
        from src.features.edit.core.custom_transitions import (
            user_transition_registry,
        )
        user_entries = [
            (entry.label, entry.type_id, entry.base, None)
            for entry in user_transition_registry().values()
        ]
        if user_entries:
            grouped[I18N.tr(
                "edit.effects.my_transitions", "My Transitions")] = user_entries

        # Audio transitions — crossfades between adjacent audio clips. They share
        # one preview icon (audio_crossfade) since the difference is the curve,
        # not the visual. Payload is the full "audio:<curve>" transition_type,
        # which the card drag handler routes to MIME_TYPE_AUDIO_TRANSITION.
        audio_entries: List[tuple[str, str, str, Optional[str]]] = [
            (d.label, d.transition_type, "audio_crossfade", None)
            for d in AUDIO_TRANSITION_DEFINITIONS
        ]
        grouped[I18N.tr("edit.effects.audio_transitions", "Audio Transitions")] = audio_entries

        for category, entries in grouped.items():
            self._build_transition_section(tree, category, entries)

        tree.expandAll()
        self._schedule_transition_preview_sync()

    def _populate_titles(self, tree: QTreeWidget) -> None:
        """Populate title templates and the reusable caption-effect library."""
        # AUD-19807: CaptionStylePreset 与 BUILTIN_PRESETS 一并局部导入，
        # 1908 行的局部注解此前引用了未导入的名字（静态检查报 undefined-name）。
        from src.features.edit.core.captions.style_preset import (
            BUILTIN_PRESETS,
            CaptionStylePreset,
        )
        from src.features.edit.core.captions.graphics import (
            DEFAULT_CAPTION_GRAPHIC_TEMPLATES,
        )
        from src.features.edit.core.title_templates import (
            list_user_title_templates,
        )

        tree.clear()
        effect_root = QTreeWidgetItem([
            I18N.tr("edit.effects.caption_effects", "Caption Effects")
        ])
        tree.addTopLevelItem(effect_root)
        for template in DEFAULT_CAPTION_GRAPHIC_TEMPLATES.all():
            item = QTreeWidgetItem([template.label or template.template_id])
            item.setData(0, _CAPTION_EFFECT_ROLE, template.template_id)
            item.setToolTip(0, template.description or template.template_id)
            effect_root.addChild(item)
        effect_root.setExpanded(True)

        grouped: Dict[str, List[CaptionStylePreset]] = {}
        for preset in BUILTIN_PRESETS:
            if preset.category in (None, "subtitle"):
                continue  # plain subtitle styles are not listed under Titles
            grouped.setdefault(preset.category, []).append(preset)
        user_templates = list_user_title_templates()
        if user_templates:
            grouped[I18N.tr("edit.effects.my_templates", "My Templates")] = user_templates

        category_labels = {
            "title": I18N.tr("edit.effects.titles_cat_title", "Title"),
            "lower_third": I18N.tr("edit.effects.titles_cat_lower", "Lower Third"),
            "callout": I18N.tr("edit.effects.titles_cat_callout", "Callout"),
            "credit": I18N.tr("edit.effects.titles_cat_credit", "Credits"),
            "social": I18N.tr("edit.effects.titles_cat_social", "Social Vertical"),
            I18N.tr("edit.effects.my_templates", "My Templates"):
                I18N.tr("edit.effects.my_templates", "My Templates"),
        }
        for category, presets in grouped.items():
            label = category_labels.get(category, str(category))
            top = QTreeWidgetItem([label])
            tree.addTopLevelItem(top)
            for preset in presets:
                item = QTreeWidgetItem([preset.label])
                item.setData(0, _TITLE_PRESET_ROLE, preset.preset_id)
                item.setToolTip(0, preset.description or preset.label)
                top.addChild(item)
            top.setExpanded(True)

    def _on_title_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """Apply a selected caption effect, or create a title from a template."""
        effect_id = item.data(0, _CAPTION_EFFECT_ROLE)
        if effect_id:
            clip_id = self._selected_subtitle_clip_id
            if not clip_id:
                self.info_label.setText(I18N.tr(
                    "edit.effects.caption_select_first",
                    "Select a subtitle clip on the timeline first, then "
                    "double-click a caption effect.",
                ))
                return
            self.caption_effect_apply_requested.emit(
                str(effect_id), clip_id, self._caption_effect_parameters()
            )
            self.info_label.setText(I18N.tr(
                    "edit.effects.caption_applied",
                    "Caption effect applied: {name}", name=item.text(0),
                ))
            return

        preset_id = item.data(0, _TITLE_PRESET_ROLE)
        if preset_id:
            self.title_template_selected.emit(str(preset_id))

    def _caption_effect_parameters(self) -> dict[str, Any]:
        """Return explicit user overrides for an effect application."""
        return {
            "in_frames": int(self.caption_effect_in_frames.value()),
            "out_frames": int(self.caption_effect_out_frames.value()),
            "distance": float(self.caption_effect_distance.value()) / 100.0,
        }

    def _on_search_changed(self, _text: str) -> None:
        """Filter effects by search text (debounced).

        AUD-19803: ``textChanged`` fires per keystroke and each fire used to
        destroy and rebuild the whole Video tree (cards, SVG rasterization,
        per-effect file IO). Restarting a 200 ms single-shot timer coalesces
        a burst of keystrokes into one rebuild.
        """
        if self._updating:
            # AUD-19804: 程序化更新（clear() / 收起搜索框）期间不重入。
            return
        self._search_debounce_timer.start()

    def _apply_search_filter(self) -> None:
        """Rebuild the Video tree for the current search text (debounce fire)."""
        text = self.search_input.text()
        if not text:
            # Show all effects
            self._populate_video_effects(self.video_effects_tree)
            return

        # Search effects
        matching_effects = self._registry.search(text)

        # Rebuild tree with only matching effects in the same responsive grid
        self._close_motion_graphic_hover_previews(self.video_effects_tree)
        self._unbind_responsive_sections(self.video_effects_tree)
        self.video_effects_tree.clear()
        matching_effects = [
            effect_def
            for effect_def in matching_effects
            if effect_def.category != "audio"
            and effect_def.effect_type not in NON_ATTACHABLE_VIDEO_EFFECT_TYPES
        ]
        self._build_effect_section(self.video_effects_tree, "filter", matching_effects)
        self._append_motion_graphic_generators(self.video_effects_tree, filter_text=text)
        self.video_effects_tree.expandAll()

    def _on_effect_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle double-click on effect item.

        FX-C06: Generators (Adjustment Clip / Solid Color) are clip TEMPLATES,
        not effects — they create a new clip when dragged onto the timeline.
        Double-clicking to ATTACH one would produce a useless EffectInstance
        that the renderer silently drops. Block the attach here with a clear
        prompt instead of emitting ``effect_add_requested``.
        """
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "effect":
            # Double-clicked on category, ignore
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)

        # Generators must never take the attach path.
        if effect_type in GENERATOR_EFFECT_TYPES:
            logger.debug(
                f"Generator double-clicked: {effect_type} — attach blocked, "
                f"prompting drag-to-timeline"
            )
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.generator_drag_hint",
                    "Drag generators onto the timeline to create a clip",
                )
            )
            return

        if effect_type and self._selected_clip_id:
            # Emit signal to add effect
            logger.debug(
                f"Effect double-clicked: {effect_type} -> clip {self._selected_clip_id}"
            )
            self.effect_add_requested.emit(effect_type, self._selected_clip_id)
        else:
            logger.debug(
                f"Effect double-clicked: {effect_type}, but no clip selected"
            )
            # Update info label to prompt user
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.no_clip_selected",
                    "Select a clip first, then double-click effect"
                )
            )

    def _on_transition_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle double-click on a transition item (P1-06).

        Emits ``transition_apply_requested`` so the controller can resolve a
        drop point (selected clip's cut first, playhead nearest-cut fallback)
        and run the same AddTransitionCommand as the drag-drop path. The
        panel never touches the timeline directly; feedback about why no
        transition could be applied comes back via
        :meth:`show_transition_feedback`.
        """
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "transition":
            return

        transition_payload = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not transition_payload:
            return

        logger.debug(
            f"Transition double-clicked: {transition_payload} "
            f"(clip: {self._selected_clip_id or 'playhead fallback'})"
        )
        self.transition_apply_requested.emit(
            transition_payload, self._selected_clip_id or ""
        )

    def show_transition_feedback(self, message: str) -> None:
        """Show why a transition could not be applied (never silently fail)."""
        if message:
            self.info_label.setText(message)

    def _start_effect_drag(self, supported_actions: Qt.DropAction) -> None:
        """
        Start drag operation for effect item.

        This is called by the tree widget when dragging starts.
        """
        # Override the tree's startDrag method
        selected_items = self.video_effects_tree.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "effect":
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)

        # Create drag object
        drag = QDrag(self)

        # Create MIME data with effect type
        mime_data = QMimeData()
        mime_data.setData(MIME_TYPE_EFFECT, QByteArray(effect_type.encode('utf-8')))
        drag.setMimeData(mime_data)

        # Execute drag
        drag.exec(Qt.DropAction.CopyAction)

        logger.debug(f"Started drag for effect: {effect_type}")

    def set_selected_clip(self, clip_id: Optional[str]) -> None:
        """
        Set the currently selected clip ID.

        This is used for double-click effect application.

        Args:
            clip_id: Clip ID or None to clear selection
        """
        self._selected_clip_id = clip_id
        self._selected_subtitle_clip_id = None

        if clip_id:
            # Show short clip ID (first 8 chars)
            short_id = clip_id[:8] if len(clip_id) > 8 else clip_id
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.info_with_clip",
                    f"Double-click or drag to apply to clip {short_id}..."
                )
            )
        else:
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.info",
                    "Double-click or drag to apply effect"
                )
            )

    def set_selected_subtitle_clip(self, clip_id: Optional[str]) -> None:
        """Set the subtitle cue targeted by the Titles-tab effect library."""
        self._selected_subtitle_clip_id = clip_id
        self._selected_clip_id = None
        if clip_id:
            short_id = clip_id[:8] if len(clip_id) > 8 else clip_id
            self.info_label.setText(I18N.tr(
                "edit.effects.subtitle_selected_hint",
                "Subtitle {clip} selected: double-click under Titles > "
                "Caption Effects to apply.", clip=short_id,
            ))
        else:
            self.info_label.setText(
                I18N.tr(
                    "edit.effects.info",
                    "Double-click or drag to apply effect"
                )
            )

    def clear(self) -> None:
        """Clear the effects panel state."""
        self._selected_clip_id = None
        self._selected_subtitle_clip_id = None
        # AUD-19804: search_input.clear() 是程序化更新，必须在 _updating 锁内
        # 进行（否则 textChanged("") 重入触发一次额外全量重建），随后停掉
        # debounce 定时器，仅保留下面这一次显式重建。
        self._updating = True
        try:
            self.search_input.clear()
        finally:
            self._updating = False
        self._search_debounce_timer.stop()
        self._populate_video_effects(self.video_effects_tree)
        self.info_label.setText(
            I18N.tr("edit.effects.info", "Double-click or drag to apply effect")
        )
        logger.debug("EditEffectsPanel.clear() called")

    # Override startDrag for the video effects tree
    # Note: This is set up in _populate_video_effects by modifying the tree
    # We need to inject the method after tree creation


# ================================================================
# Drag Drop Helper Functions
# ================================================================

def is_effect_mime_data(mime_data: QMimeData) -> bool:
    """
    Check if MIME data contains an effect.

    Args:
        mime_data: MIME data from drag event

    Returns:
        True if data contains an effect type
    """
    return mime_data.hasData(MIME_TYPE_EFFECT)


def get_effect_type_from_mime_data(mime_data: QMimeData) -> Optional[str]:
    """
    Get effect type from MIME data.

    Args:
        mime_data: MIME data from drag event

    Returns:
        Effect type string or None if not found
    """
    if not is_effect_mime_data(mime_data):
        return None

    data = mime_data.data(MIME_TYPE_EFFECT)
    return bytes(data).decode('utf-8')


def is_transition_mime_data(mime_data: QMimeData) -> bool:
    """
    Check if MIME data contains a transition.

    Args:
        mime_data: MIME data from drag event

    Returns:
        True if data contains a transition type
    """
    return mime_data.hasData(MIME_TYPE_TRANSITION)


def get_transition_type_from_mime_data(mime_data: QMimeData) -> Optional[str]:
    """
    Get transition type from MIME data.

    Args:
        mime_data: MIME data from drag event

    Returns:
        Transition type string or None if not found
    """
    if not is_transition_mime_data(mime_data):
        return None

    data = mime_data.data(MIME_TYPE_TRANSITION)
    return bytes(data).decode('utf-8')


def is_generator_mime_data(mime_data: QMimeData) -> bool:
    """Check if MIME data carries a generator template (Adjustment/Solid)."""
    return mime_data.hasData(MIME_TYPE_GENERATOR)


def get_generator_type_from_mime_data(mime_data: QMimeData) -> Optional[str]:
    """Get the generator effect_type from MIME data, or None."""
    if not is_generator_mime_data(mime_data):
        return None
    data = mime_data.data(MIME_TYPE_GENERATOR)
    return bytes(data).decode('utf-8')


# Inject custom startDrag method into the video_effects_tree
# This is done at module level to ensure drag support works
def _inject_drag_support(panel: EditEffectsPanel) -> None:
    """Inject custom drag support into panel's effect tree."""
    def custom_start_drag(supported_actions: Qt.DropAction) -> None:
        selected_items = panel.video_effects_tree.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "effect":
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)

        # Generators (Adjustment Clip / Solid Color) drag to CREATE a new clip,
        # so they carry a distinct MIME the timeline routes to its generator
        # drop handler rather than the effect-attach handler.
        from src.features.edit.core.effect_registry import GENERATOR_EFFECT_TYPES
        mime_key = (
            MIME_TYPE_GENERATOR if effect_type in GENERATOR_EFFECT_TYPES
            else MIME_TYPE_EFFECT
        )

        drag = QDrag(panel)
        mime_data = QMimeData()
        mime_data.setData(mime_key, QByteArray(effect_type.encode('utf-8')))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

        logger.debug(f"Started drag for effect: {effect_type}")

    # Replace the tree's startDrag method
    panel.video_effects_tree.startDrag = custom_start_drag


# Also inject for transitions tree
def _inject_transition_drag_support(panel: EditEffectsPanel) -> None:
    """Inject custom drag support into panel's transition tree."""
    def custom_start_drag(supported_actions: Qt.DropAction) -> None:
        selected_items = panel.transitions_tree.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "transition":
            return

        transition_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        direction = item.data(0, Qt.ItemDataRole.UserRole + 2)
        payload = f"wipe:{direction}" if transition_type == "wipe" and direction else transition_type

        # Audio transitions (audio:*) carry their own MIME so the timeline drop
        # router can target audio tracks.
        mime_type = (
            MIME_TYPE_AUDIO_TRANSITION if is_audio_transition(payload)
            else MIME_TYPE_TRANSITION
        )
        drag = QDrag(panel)
        mime_data = QMimeData()
        mime_data.setData(mime_type, QByteArray(payload.encode('utf-8')))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

        logger.debug(f"Started drag for transition ({mime_type}): {transition_type}")

    panel.transitions_tree.startDrag = custom_start_drag


# P7 Completion: Inject drag support for audio effects tree
def _inject_audio_drag_support(panel: EditEffectsPanel) -> None:
    """Inject custom drag support into panel's audio effects tree."""
    def custom_start_drag(supported_actions: Qt.DropAction) -> None:
        selected_items = panel.audio_effects_tree.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "effect":
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)

        drag = QDrag(panel)
        mime_data = QMimeData()
        mime_data.setData(MIME_TYPE_EFFECT, QByteArray(effect_type.encode('utf-8')))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

        logger.debug(f"Started drag for audio effect: {effect_type}")

    panel.audio_effects_tree.startDrag = custom_start_drag


# P7 Completion: Inject drag support for favorites tree
def _inject_favorites_drag_support(panel: EditEffectsPanel) -> None:
    """Inject custom drag support into panel's favorites tree."""
    def custom_start_drag(supported_actions: Qt.DropAction) -> None:
        selected_items = panel.favorites_tree.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        item_type = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type != "effect":
            return

        effect_type = item.data(0, Qt.ItemDataRole.UserRole + 1)

        drag = QDrag(panel)
        mime_data = QMimeData()
        mime_data.setData(MIME_TYPE_EFFECT, QByteArray(effect_type.encode('utf-8')))
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

        logger.debug(f"Started drag for favorite effect: {effect_type}")

    panel.favorites_tree.startDrag = custom_start_drag
