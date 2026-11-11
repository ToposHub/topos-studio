"""Compact Motion Graphic editor skin built from the application's UI palette."""

from src.shared.ui.style_sheet import C
from src.features.edit.ui.edit_style import EDIT_COLORS


MOTION_GRAPHIC_EDITOR_QSS = f"""
QMainWindow#MotionGraphicEditor {{
    background: {C.BG_DEEPEST};
    color: {C.TEXT_PRIMARY};
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    font-size: 12px;
}}
QWidget#MotionGraphicEditor {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    font-size: 12px;
}}
/* The application stylesheet also styles generic QLabel controls.  Make text
   in MG explicitly transparent so titles and metadata never look like small,
   unrelated dark buttons. */
QLabel {{
    color: {C.TEXT_SECONDARY};
    font-size: 12px;
    background: transparent;
    border: none;
}}
QToolBar#motionGraphicToolbar {{
    background: {C.BG_DARK};
    border: none;
    border-bottom: 1px solid {C.BORDER_DARK};
    spacing: 0px;
    padding: 3px 6px;
}}
QToolBar#motionGraphicToolbar QWidget {{
    background: transparent;
}}
QToolButton#mgMenuButton, QToolButton#mgQuickToolButton,
QToolButton#mgExportButton {{
    color: {C.TEXT_SECONDARY};
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    padding: 3px 8px;
    min-height: 20px;
}}
QToolButton#mgMenuButton:hover, QToolButton#mgQuickToolButton:hover {{
    color: {C.TEXT_PRIMARY};
    background: {C.BG_BUTTON};
}}
QToolButton#mgMenuButton:pressed, QToolButton#mgQuickToolButton:pressed {{
    background: {C.BG_BUTTON_PRESS};
}}
QToolButton#mgMenuButton::menu-indicator {{
    image: none;
}}
QToolButton#mgExportButton {{
    background: {C.ORANGE_PRESS};
    color: #ffffff;
    padding-left: 13px;
    padding-right: 13px;
}}
QToolButton#mgExportButton:hover {{ background: {C.ORANGE_HOVER}; }}
QToolButton#mgExportButton:pressed {{ background: {C.ORANGE}; }}
QMenu {{
    background: rgba(39, 39, 43, 245);
    border: 1px solid {C.BORDER_MID};
    border-radius: 4px;
    padding: 3px 0px;
    color: {C.TEXT_SECONDARY};
    font-size: 12px;
}}
QMenu::item {{
    padding: 4px 36px 4px 14px;
}}
QMenu::item:selected {{
    background: {C.ORANGE_PRESS};
    color: #ffffff;
}}
QMenu::separator {{
    height: 1px;
    background: {C.BORDER_MID};
    margin: 3px 8px;
}}
QFrame#mgPanel, QFrame#mgPreview {{
    background: {C.BG_PANEL};
    border: 1px solid {C.BORDER_DARK};
    border-radius: 5px;
}}
QFrame#mgPageStrip, QFrame#mgTimeline {{
    background: {EDIT_COLORS["bg_panel"]};
    border: 1px solid {EDIT_COLORS["border_subtle"]};
    border-radius: 4px;
}}
QFrame#mgPreview {{
    background: {C.BG_DEEPEST};
}}
QWidget#mgPreviewToolStrip {{
    background: transparent;
}}
/* Match Edit > Timeline's compact icon-tool interaction.  State is conveyed
   by the orange icon, not a large coloured button behind the preview. */
QToolButton#mgPreviewToolButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px;
    margin: 0px;
}}
QToolButton#mgPreviewToolButton:hover {{
    background-color: rgba(255, 255, 255, 18);
}}
QToolButton#mgPreviewToolButton:pressed {{
    background-color: rgba(255, 255, 255, 10);
}}
QToolButton#mgPreviewToolButton:checked {{
    background: transparent;
}}
QToolButton#mgPreviewToolButton:checked:hover {{
    background-color: rgba(255, 255, 255, 18);
}}
QLabel#mgSectionTitle {{
    color: {C.TEXT_PRIMARY};
    font-size: 10px;
    font-weight: 600;
    padding: 1px 2px;
    background: transparent;
    border: none;
}}
QLabel#mgMutedLabel {{
    color: {C.TEXT_HINT};
    font-size: 9px;
    background: transparent;
    border: none;
}}
QLabel#mgInspectorSectionLabel {{
    color: {C.TEXT_HINT};
    font-size: 10px;
    font-weight: 600;
    padding-top: 4px;
    background: transparent;
    border: none;
}}
QLabel#mgSelectionLabel {{
    color: {C.TEXT_HINT};
    font-size: 9px;
    padding: 1px 0;
    background: transparent;
    border: none;
}}
QTabWidget#mgPanelTabs::pane {{
    border: none;
    background: {C.BG_PANEL};
}}
/* Same compact segmented tabs used when project panels are merged. */
QTabWidget#mgPanelTabs > QTabBar::tab {{
    background: rgb(70, 70, 77);
    color: #bbbbbb;
    border: none;
    padding: 3px 14px;
    font-size: 12px;
}}
QTabWidget#mgPanelTabs > QTabBar::tab:hover {{
    color: #bbbbbb;
    background: rgba(255, 255, 255, 15);
}}
QTabWidget#mgPanelTabs > QTabBar::tab:selected {{
    color: #ffffff;
    background: rgb(204, 102, 0);
}}
QTabWidget#mgPanelTabs > QTabBar::tab:first {{
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}}
QTabWidget#mgPanelTabs > QTabBar::tab:last {{
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}}
QTabWidget#mgPanelTabs > QTabBar::tab:only-one {{
    border-radius: 8px;
}}
QFrame#mgLayerHeader {{
    background: {C.BG_DARK};
    border: none;
    border-bottom: 1px solid {C.BORDER_DARK};
}}
QLabel#mgLayerPanelTitle {{
    color: {C.TEXT_SECONDARY};
    font-size: 10px;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QListWidget#mgLayerList {{
    padding: 0;
}}
QListWidget#mgLayerList::item {{
    min-height: 24px;
    padding: 0;
    border-radius: 0;
    border-bottom: 1px solid rgba(0, 0, 0, 28);
}}
QLabel#mgLayerRowLabel {{
    color: {C.TEXT_PRIMARY};
    padding-left: 4px;
}}
QToolButton#mgLayerRowTool, QToolButton#mgLayerFooterButton {{
    background: transparent;
    border: none;
    border-radius: 3px;
}}
QToolButton#mgLayerRowTool:hover, QToolButton#mgLayerFooterButton:hover {{
    background: rgba(255, 255, 255, 18);
}}
QToolButton#mgLayerRowTool:checked {{
    background: rgba(255, 136, 0, 30);
}}
QToolButton#mgLayerFooterButton:disabled {{
    opacity: 0.35;
}}
QFrame#mgLayerFooter {{
    background: transparent;
    border: none;
}}
QListWidget, QTreeView, QScrollArea {{
    background: {C.BG_PANEL};
    color: {C.TEXT_SECONDARY};
    border: none;
    outline: none;
}}
QListWidget::item {{ padding: 5px 6px; border-radius: 3px; }}
QListWidget::item:hover {{ background: {C.BG_HOVER}; }}
QListWidget::item:selected {{ background: {C.ACCENT_SURFACE}; color: #ffffff; }}
/* Small explicit action buttons used by Source and DevTools.  They override
   broad application QPushButton rules, which otherwise add a mismatched box. */
QPushButton#mgPrimaryButton, QPushButton#mgDevToolsOpenButton {{
    color: #ffffff;
    background: {C.ORANGE_PRESS};
    border: 1px solid {C.ORANGE};
    border-radius: 4px;
    padding: 3px 8px;
    min-height: 20px;
}}
QPushButton#mgPrimaryButton:hover, QPushButton#mgDevToolsOpenButton:hover {{
    background: {C.ORANGE_HOVER};
}}
QPushButton#mgDevToolsApplyButton, QPushButton#mgDevToolsDiscardButton {{
    color: {C.TEXT_SECONDARY};
    background: {C.BG_BUTTON};
    border: 1px solid {C.BORDER};
    border-radius: 4px;
    padding: 3px 8px;
    min-height: 20px;
}}
QPushButton#mgDevToolsApplyButton:hover, QPushButton#mgDevToolsDiscardButton:hover {{
    color: {C.TEXT_PRIMARY};
    background: {C.BG_BUTTON_HOVER};
    border-color: {C.BORDER_HOVER};
}}
QPushButton#mgDevToolsApplyButton:disabled, QPushButton#mgDevToolsDiscardButton:disabled {{
    color: {C.TEXT_HINT};
    background: {C.BG_DARK};
    border-color: {C.BORDER_DARK};
}}
/* Inputs, sliders, checks and ordinary buttons deliberately inherit the
   application stylesheet.  The clip Properties inspector is the canonical
   interaction surface; redefining those controls here caused a second slider
   design (orange progress bar + white handle) inside Motion Graphics. */
QToolButton#mgPageTimingJump {{
    color: {C.TEXT_SECONDARY};
    background: {C.BG_BUTTON};
    border: 1px solid {C.BORDER};
    border-radius: 4px;
    padding: 2px 6px;
    min-width: 54px;
}}
QToolButton#mgPageTimingJump:hover {{
    color: {C.TEXT_PRIMARY};
    background: {C.BG_BUTTON_HOVER};
    border-color: {C.BORDER_HOVER};
}}
QStatusBar {{ background: {C.BG_DARK}; color: {C.TEXT_HINT}; border-top: 1px solid {C.BORDER_DARK}; }}
QWebEngineView {{ background: {C.BG_DEEPEST}; border: none; }}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    border: none;
    width: 8px;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {C.BORDER_MID};
    border-radius: 4px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{ background: {C.TEXT_HINT}; }}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; border: none; }}
QToolButton#mgPageButton {{
    color: {C.TEXT_SECONDARY};
    background: {C.BG_BUTTON};
    border: 1px solid {C.BORDER};
    border-radius: 4px;
    padding: 3px 8px;
    min-width: 62px;
}}
QToolButton#mgPageButton:hover {{ background: {C.BG_BUTTON_HOVER}; color: #ffffff; }}
QToolButton#mgPageButton:checked {{ background: {C.ACCENT_SURFACE}; border-color: {C.ORANGE}; color: #ffffff; }}
QToolButton#mgTimelinePageStepButton, QToolButton#mgTimelinePlayButton,
QToolButton#mgTimelineMarkerButton, QToolButton#mgTimelineLoopButton {{
    color: {C.TEXT_SECONDARY};
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px;
}}
QToolButton#mgTimelinePageStepButton:hover, QToolButton#mgTimelinePlayButton:hover,
QToolButton#mgTimelineMarkerButton:hover, QToolButton#mgTimelineLoopButton:hover {{
    background: rgba(255, 255, 255, 18);
}}
QToolButton#mgTimelinePageStepButton:pressed, QToolButton#mgTimelinePlayButton:pressed,
QToolButton#mgTimelineMarkerButton:pressed, QToolButton#mgTimelineLoopButton:pressed {{
    background: rgba(255, 255, 255, 10);
}}
QToolButton#mgTimelineLoopButton:checked {{ color: {C.ORANGE}; background: rgba(255, 136, 0, 30); }}
QLabel#mgTimelineTimecode {{
    color: {C.TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
    background: {C.BG_INPUT};
    padding: 3px 8px;
    border-radius: 4px;
    font-family: "Courier New", monospace;
}}
QPushButton#mgPageTimingApplyButton {{
    background: {C.ORANGE_PRESS};
    color: #ffffff;
    border: 1px solid {C.ORANGE};
    border-radius: 4px;
    padding: 3px 10px;
    min-height: 20px;
}}
QPushButton#mgPageTimingApplyButton:hover {{ background: {C.ORANGE_HOVER}; }}
QPushButton#mgPageTimingApplyButton:pressed {{ background: {C.ORANGE}; }}

/* Asset library panel (preset browser).  The caption font is 10px, set BOTH
   here and in code on the view: the editor-wide QWidget#MotionGraphicEditor
   rule above cascades a 12px font over every child, and a stylesheet font
   silently overrides any programmatic setFont — the code path alone was why
   the caption size never seemed to change.  ::item font-size is still
   avoided (does not propagate to item text); the widget rule does.  Keep
   this in sync with _CAPTION_PIXELS in asset_library_panel.py. */
QListWidget#mgLibraryGrid {{
    background: {C.BG_DEEPEST};
    border: 1px solid #1f2937;
    border-radius: 6px;
    outline: none;
    font-size: 10px;
}}
QListWidget#mgLibraryGrid::item {{
    color: #cbd5f5;
    border-radius: 6px;
    padding: 1px;
}}
QListWidget#mgLibraryGrid::item:selected {{
    background: #1c2a45;
    border: 1px solid {C.ORANGE};
    color: #ffffff;
}}
QLabel#mgLibraryName {{
    color: #ffffff;
    font-weight: 600;
}}
/* Collapsed-search toggle: same transparent idiom as the preview tools. */
QToolButton#mgLibrarySearchToggle {{
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px;
}}
QToolButton#mgLibrarySearchToggle:hover {{
    background-color: rgba(255, 255, 255, 18);
}}
QToolButton#mgLibrarySearchToggle:checked {{
    background: {C.ORANGE};
}}
/* Keyframes timeline (bottom panel): the two halves paint their own dark
   canvas, so the QSS only styles the toolbar strip and its compact buttons. */
QWidget#mgKeyframesPanel {{
    background: {EDIT_COLORS["bg_panel"]};
    border-top: 1px solid {EDIT_COLORS["border_subtle"]};
}}
QWidget#mgKeyframesToolbar {{
    background: {EDIT_COLORS["bg_toolbar"]};
    border-bottom: 1px solid {EDIT_COLORS["border_subtle"]};
}}
QToolButton#mgKeyframesToolButton, QToolButton#mgKeyframesFilterButton {{
    color: {EDIT_COLORS["text_muted"]};
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px;
    font-size: 12px;
}}
QToolButton#mgKeyframesFilterButton {{
    padding: 3px 8px;
    text-align: left;
}}
QToolButton#mgKeyframesToolButton:hover, QToolButton#mgKeyframesFilterButton:hover {{
    background: rgba(255, 255, 255, 18);
    color: {EDIT_COLORS["text"]};
}}
QToolButton#mgKeyframesToolButton:pressed, QToolButton#mgKeyframesFilterButton:pressed {{
    background: rgba(255, 255, 255, 10);
}}
QToolButton#mgKeyframesToolButton:disabled, QToolButton#mgKeyframesFilterButton:disabled {{
    color: {EDIT_COLORS["text_muted"]};
}}
/* One shared slider recipe for the whole editor (timeline zoom + library
   thumbnail zoom) — keeps every horizontal slider reading as one control
   family. */
QSlider#mgKeyframesZoomSlider, QSlider#mgLibraryZoom {{
    background: transparent;
}}
QSlider#mgKeyframesZoomSlider::groove:horizontal,
QSlider#mgLibraryZoom::groove:horizontal {{
    border: 1px solid {EDIT_COLORS["border"]};
    height: 4px;
    background: {EDIT_COLORS["bg_input"]};
    margin: 2px 0;
    border-radius: 2px;
}}
QSlider#mgKeyframesZoomSlider::sub-page:horizontal,
QSlider#mgKeyframesZoomSlider::add-page:horizontal,
QSlider#mgLibraryZoom::sub-page:horizontal,
QSlider#mgLibraryZoom::add-page:horizontal {{
    background: transparent;
    border: none;
}}
QSlider#mgKeyframesZoomSlider::handle:horizontal,
QSlider#mgLibraryZoom::handle:horizontal {{
    background: {EDIT_COLORS["accent"]};
    border: 1px solid {EDIT_COLORS["bg_toolbar"]};
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 5px;
}}
QSlider#mgKeyframesZoomSlider::handle:horizontal:hover,
QSlider#mgLibraryZoom::handle:horizontal:hover {{
    background: {EDIT_COLORS["accent_hover"]};
}}
"""


__all__ = ["MOTION_GRAPHIC_EDITOR_QSS"]
