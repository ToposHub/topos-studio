"""Clip-Properties-style inspector for structured Motion Graphic layers."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from typing import Any, Mapping
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import (
    C, EDIT_PROPERTIES_ROW_STYLE, EDIT_PROPERTIES_SECTION_CONTENT_STYLE,
    EDIT_PROPERTIES_SECTION_DIVIDER_STYLE, EDIT_PROPERTIES_SECTION_HEADER_STYLE,
    EDIT_PROPERTIES_SECTION_STYLE, EDIT_PROPERTIES_SECTION_TITLE_STYLE,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox, ColorPickerControl, DragValue, ParamSliderRow
from src.shared.ui.widgets.color_picker_control import linear_rgba_to_qcolor, qcolor_to_linear_rgba
from src.shared.ui.widgets.topos_controls import ToposClickableSlider

from ..core.model import SceneElement, SceneTransform


_VALUE_WIDTH = 64

_FONT_WEIGHTS = (
    ("normal", "Normal"), ("bold", "Bold"), ("lighter", "Lighter"), ("bolder", "Bolder"),
    ("300", "Light 300"), ("400", "Regular 400"), ("500", "Medium 500"),
    ("600", "Semibold 600"), ("700", "Bold 700"), ("800", "Extrabold 800"),
    ("900", "Black 900"),
)
_TEXT_ALIGNS = (
    ("left", "Left"), ("center", "Center"), ("right", "Right"), ("justify", "Justify"),
)
_BLEND_MODES = (
    ("normal", "Normal"), ("multiply", "Multiply"), ("screen", "Screen"),
    ("overlay", "Overlay"), ("darken", "Darken"), ("lighten", "Lighten"),
    ("difference", "Difference"),
)

_LAYOUT_MODES = (
    ("absolute", "Absolute"), ("flex-row", "Flex Row"),
    ("flex-column", "Flex Column"), ("grid", "Grid"),
)
_LAYOUT_JUSTIFY = (
    ("start", "Start"), ("center", "Center"), ("end", "End"),
    ("between", "Between"), ("around", "Around"), ("evenly", "Evenly"),
)
_LAYOUT_ALIGN = (
    ("start", "Start"), ("center", "Center"), ("end", "End"), ("stretch", "Stretch"),
)

_HORIZONTAL_CONSTRAINTS = (
    ("left", "Left"), ("center", "Center"), ("right", "Right"), ("scale", "Scale"),
)
_VERTICAL_CONSTRAINTS = (
    ("top", "Top"), ("center", "Center"), ("bottom", "Bottom"), ("scale", "Scale"),
)
_SIZE_POLICIES = (
    ("fixed", "Fixed"), ("fill", "Fill"), ("hug", "Hug"),
)


class _RatioButton(QToolButton):
    """One quick-preview ratio button; clicking reports its canvas size."""

    def __init__(self, label: str, width: int, height: int,
                 on_activate, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = (int(width), int(height))
        self.setText(label)
        self.setToolTip(f"{label} — {width} × {height}")
        self.setCheckable(False)
        self.setStyleSheet(
            "QToolButton { color: #bbbbbb; background: rgba(255,255,255,12);"
            " border: none; border-radius: 3px; padding: 3px 10px; }"
            "QToolButton:hover { color: #ffffff; background: rgba(255,255,255,26); }")
        self.clicked.connect(lambda: on_activate(*self._size))


class _RatioButtons(QWidget):
    """Horizontal row of quick-preview ratio buttons (16:9 / 9:16 / 1:1)."""

    def __init__(self, on_activate, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        from ..core.constraints import QUICK_PREVIEW_RATIOS
        for name, width, height in QUICK_PREVIEW_RATIOS:
            row.addWidget(_RatioButton(name, width, height, on_activate, self))
        row.addStretch(1)


class SceneInspector(QWidget):
    """Structured-layer parameters using the Edit > Properties control system."""

    elementUpdateRequested = pyqtSignal(str, object)
    selectionUpdateRequested = pyqtSignal(object, object)
    keyframeRequested = pyqtSignal(str, str, float)
    canvasSizeRequested = pyqtSignal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._element: SceneElement | None = None
        self._selection_ids: tuple[str, ...] = ()
        self._scene: Any = None
        self._tokens: Any = None
        self._style_clipboard: dict | None = None
        self._frame = 0
        self._updating = False
        # Deferred style commits: sliders record only the fields the user
        # touched so one gesture becomes one undo command and a multi-select
        # batch never overwrites unrelated fields with the primary's values.
        self._pending_style_fields: set[str] = set()
        self._pending_layout_fields: set[str] = set()
        self._pending_shape_change: str | None = None
        self._pending_transform_change = False

        self._host = QWidget(self)
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.selection_label = QLabel(self._host)
        self.selection_label.setObjectName("mgSelectionLabel")
        self._layout.addWidget(self.selection_label)

        content = self._add_section(I18N.tr("motion_graphic.sections.content", "Content"))
        self.content_edit = QLineEdit(self._host)
        self._content_row = self._add_field_row(content, I18N.tr("motion_graphic.sections.content", "Content"), self.content_edit)

        text = self._add_section(I18N.tr("motion_graphic.sections.text", "Text"))
        self._text_section = text.parentWidget().parentWidget() if text.parentWidget() is not None else self._host
        self.font_edit = QLineEdit(self._host)
        self._font_row = self._add_field_row(text, I18N.tr("motion_graphic.scene.font", "Font"), self.font_edit)
        self.font_size = self._add_style_number_row(
            text, I18N.tr("motion_graphic.text.font_size", "Font Size"), 8.0, 512.0, 48.0, 1.0, 0)
        self.font_weight = ChevronComboBox(self._host)
        for value, label in _FONT_WEIGHTS:
            self.font_weight.addItem(label, value)
        self._add_field_row(text, I18N.tr("motion_graphic.text.font_weight", "Weight"), self.font_weight)
        self.text_align = ChevronComboBox(self._host)
        for value, label in _TEXT_ALIGNS:
            self.text_align.addItem(label, value)
        self._add_field_row(text, I18N.tr("motion_graphic.text.align", "Alignment"), self.text_align)
        self.line_height = self._add_style_number_row(
            text, I18N.tr("motion_graphic.text.line_height", "Line Height"), 0.5, 4.0, 1.2, 0.05, 2)
        self.letter_spacing = self._add_style_number_row(
            text, I18N.tr("motion_graphic.text.letter_spacing", "Letter Spacing"), -20.0, 100.0, 0.0, 0.5, 1)

        layout_section = self._add_section(I18N.tr("motion_graphic.sections.layout", "Layout"))
        self._layout_section = layout_section.parentWidget().parentWidget() if layout_section.parentWidget() is not None else self._host
        self.layout_mode = ChevronComboBox(self._host)
        for value, label in _LAYOUT_MODES:
            self.layout_mode.addItem(label, value)
        self._add_field_row(layout_section, I18N.tr("motion_graphic.layout.mode", "Layout"), self.layout_mode)
        self.layout_gap = self._add_style_number_row(
            layout_section, I18N.tr("motion_graphic.layout.gap", "Gap"), 0.0, 200.0, 0.0, 1.0, 0)
        self.layout_padding = self._add_style_number_row(
            layout_section, I18N.tr("motion_graphic.layout.padding", "Padding"), 0.0, 200.0, 0.0, 1.0, 0)
        self.layout_justify = ChevronComboBox(self._host)
        for value, label in _LAYOUT_JUSTIFY:
            self.layout_justify.addItem(label, value)
        self._add_field_row(layout_section, I18N.tr("motion_graphic.layout.justify", "Justify"), self.layout_justify)
        self.layout_align = ChevronComboBox(self._host)
        for value, label in _LAYOUT_ALIGN:
            self.layout_align.addItem(label, value)
        self._add_field_row(layout_section, I18N.tr("motion_graphic.layout.align", "Align"), self.layout_align)
        self.layout_columns = self._add_style_number_row(
            layout_section, I18N.tr("motion_graphic.layout.columns", "Columns"), 1.0, 12.0, 2.0, 1.0, 0)
        self.layout_rows = self._add_style_number_row(
            layout_section, I18N.tr("motion_graphic.layout.rows", "Rows"), 1.0, 12.0, 2.0, 1.0, 0)

        # R1: 响应式约束 —— 活画布与基准画布不一致时元素如何重摆。
        constraints_section = self._add_section(
            I18N.tr("motion_graphic.sections.constraints", "Constraints"))
        self._constraints_section = (constraints_section.parentWidget().parentWidget()
                                     if constraints_section.parentWidget() is not None else self._host)
        self.constraint_horizontal = ChevronComboBox(self._host)
        for value, label in _HORIZONTAL_CONSTRAINTS:
            self.constraint_horizontal.addItem(label, value)
        self._add_field_row(constraints_section,
                            I18N.tr("motion_graphic.constraints.horizontal", "Horizontal"),
                            self.constraint_horizontal)
        self.constraint_vertical = ChevronComboBox(self._host)
        for value, label in _VERTICAL_CONSTRAINTS:
            self.constraint_vertical.addItem(label, value)
        self._add_field_row(constraints_section,
                            I18N.tr("motion_graphic.constraints.vertical", "Vertical"),
                            self.constraint_vertical)
        self.constraint_keep_aspect = ChevronComboBox(self._host)
        self.constraint_keep_aspect.addItem(
            I18N.tr("motion_graphic.constraints.off", "Off"), False)
        self.constraint_keep_aspect.addItem(
            I18N.tr("motion_graphic.constraints.on", "On"), True)
        self._add_field_row(constraints_section,
                            I18N.tr("motion_graphic.constraints.keep_aspect", "Keep Aspect"),
                            self.constraint_keep_aspect)
        self.width_mode = ChevronComboBox(self._host)
        for value, label in _SIZE_POLICIES:
            self.width_mode.addItem(label, value)
        self._add_field_row(constraints_section,
                            I18N.tr("motion_graphic.constraints.width_mode", "Width Mode"),
                            self.width_mode)
        self.height_mode = ChevronComboBox(self._host)
        for value, label in _SIZE_POLICIES:
            self.height_mode.addItem(label, value)
        self._add_field_row(constraints_section,
                            I18N.tr("motion_graphic.constraints.height_mode", "Height Mode"),
                            self.height_mode)

        # R1: 画布 —— 断点与 16:9 / 9:16 / 1:1 快速预览（场景级，恒可见）。
        canvas_section = self._add_section(
            I18N.tr("motion_graphic.sections.canvas", "Canvas"))
        self._canvas_section = (canvas_section.parentWidget().parentWidget()
                                if canvas_section.parentWidget() is not None else self._host)
        self.canvas_breakpoint = ChevronComboBox(self._host)
        self._add_field_row(canvas_section,
                            I18N.tr("motion_graphic.canvas.breakpoint", "Breakpoint"),
                            self.canvas_breakpoint)
        self._add_field_row(
            canvas_section, I18N.tr("motion_graphic.canvas.quick_preview", "Quick Preview"),
            _RatioButtons(self._canvas_ratio_clicked, self))

        appearance = self._add_section(I18N.tr("motion_graphic.sections.appearance", "Appearance"))
        self._appearance_section = appearance.parentWidget().parentWidget() if appearance.parentWidget() is not None else self._host
        self.color_edit = ColorPickerControl(qcolor_to_linear_rgba(QColor("#ffffff")), self._host)
        self._color_row = self._add_field_row(appearance, I18N.tr("motion_graphic.sections.color_tokens", "Color"), self.color_edit)
        self.color_token = ChevronComboBox(self._host)
        self.color_token.addItem(I18N.tr("motion_graphic.tokens.custom_color", "Custom"), "")
        self._color_token_row = self._add_field_row(
            appearance, I18N.tr("motion_graphic.tokens.color_token", "Color Token"), self.color_token)
        self.opacity = self._add_style_number_row(
            appearance, I18N.tr("motion_graphic.appearance.opacity", "Opacity"), 0.0, 100.0, 100.0, 1.0, 0)
        self.blur = self._add_style_number_row(
            appearance, I18N.tr("motion_graphic.appearance.blur", "Blur"), 0.0, 50.0, 0.0, 0.5, 1)
        self.blend_mode = ChevronComboBox(self._host)
        for value, label in _BLEND_MODES:
            self.blend_mode.addItem(label, value)
        self._add_field_row(appearance, I18N.tr("motion_graphic.appearance.blend_mode", "Blend Mode"), self.blend_mode)
        self.shadow_x = self._add_style_number_row(
            appearance, I18N.tr("motion_graphic.appearance.shadow_x", "Shadow X"), -100.0, 100.0, 0.0, 1.0, 0)
        self.shadow_y = self._add_style_number_row(
            appearance, I18N.tr("motion_graphic.appearance.shadow_y", "Shadow Y"), -100.0, 100.0, 0.0, 1.0, 0)
        self.shadow_blur = self._add_style_number_row(
            appearance, I18N.tr("motion_graphic.appearance.shadow_blur", "Shadow Blur"), 0.0, 100.0, 0.0, 1.0, 0)
        self.shadow_color = ColorPickerControl(qcolor_to_linear_rgba(QColor("#000000")), self._host)
        self._add_field_row(appearance, I18N.tr("motion_graphic.appearance.shadow_color", "Shadow Color"), self.shadow_color)

        shape = self._add_section(I18N.tr("motion_graphic.sections.shape", "Shape"))
        self._shape_section = shape.parentWidget().parentWidget() if shape.parentWidget() is not None else self._host
        self.shape_kind = ChevronComboBox(self._host)
        for kind, label in (
            ("rect", "Rectangle"), ("rounded_rect", "Rounded Rectangle"),
            ("ellipse", "Ellipse"), ("line", "Line"), ("arrow", "Arrow"),
            ("triangle", "Triangle"), ("polygon", "Polygon"),
            ("star", "Star"), ("path", "Pen Path"),
        ):
            self.shape_kind.addItem(label, kind)
        self._shape_kind_row = self._add_field_row(shape, I18N.tr("motion_graphic.shape.kind", "Type"), self.shape_kind)
        self.stroke_edit = ColorPickerControl(qcolor_to_linear_rgba(QColor("#000000")), self._host)
        self._stroke_row = self._add_field_row(shape, I18N.tr("motion_graphic.shape.stroke", "Stroke"), self.stroke_edit)
        self.stroke_width = self._add_shape_number_row(shape, I18N.tr("motion_graphic.shape.stroke_width", "Stroke Width"), 0.0, 64.0, 0.0, 0.1, 1)
        self.corner_radius = self._add_shape_number_row(shape, I18N.tr("motion_graphic.shape.corner_radius", "Corner Radius"), 0.0, 500.0, 0.0, 1.0, 1)
        self.polygon_sides = self._add_shape_number_row(shape, I18N.tr("motion_graphic.shape.sides", "Sides"), 3.0, 32.0, 6.0, 1.0, 0)
        self.star_points = self._add_shape_number_row(shape, I18N.tr("motion_graphic.shape.points", "Points"), 3.0, 32.0, 5.0, 1.0, 0)
        self.star_inner_ratio = self._add_shape_number_row(shape, I18N.tr("motion_graphic.shape.inner_ratio", "Inner Ratio"), 0.05, 0.95, 0.45, 0.01, 2)

        transform = self._add_section(I18N.tr("motion_graphic.sections.transform", "Transform"))
        self.x_spin = self._add_number_row(transform, "X", -5000.0, 5000.0, 0.0, 2)
        self.y_spin = self._add_number_row(transform, "Y", -5000.0, 5000.0, 0.0, 2)
        self.width_spin = self._add_number_row(transform, I18N.tr("motion_graphic.transform.width", "Width"), 1.0, 8192.0, 100.0, 2)
        self.height_spin = self._add_number_row(transform, I18N.tr("motion_graphic.transform.height", "Height"), 1.0, 8192.0, 100.0, 2)
        self.scale_spin = self._add_number_row(transform, I18N.tr("motion_graphic.transform.scale", "Scale"), 0.01, 8.0, 1.0, 2)
        self.rotation_spin = self._add_number_row(transform, I18N.tr("motion_graphic.transform.rotation", "Rotation"), -360.0, 360.0, 0.0, 1)
        self.keyframe_button = QToolButton(self._host)
        self.keyframe_button.setText(I18N.tr("motion_graphic.scene.opacity_keyframe", "Add Opacity Keyframe"))
        self.keyframe_button.setToolTip(I18N.tr("motion_graphic.scene.opacity_keyframe_tooltip", "Add an opacity keyframe at the current frame"))
        self.keyframe_button.setStyleSheet(
            "QToolButton { color: #aaaaaa; background: transparent; border: none; padding: 4px 8px; }"
            "QToolButton:hover { color: #ffffff; background: rgba(255,255,255,18); border-radius: 3px; }")
        transform.addWidget(self.keyframe_button)

        animation = self._add_section(I18N.tr("motion_graphic.sections.animation", "Animation"))
        self.animation_summary = QLabel(self._host)
        self.animation_summary.setObjectName("mgAnimationSummary")
        self.animation_summary.setWordWrap(True)
        self.animation_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.animation_summary.setStyleSheet(
            "QLabel#mgAnimationSummary { color: #9aa4b2; padding: 2px; font-size: 11px; }")
        animation.addWidget(self.animation_summary)
        self._layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(self._host)
        scroll.setWidgetResizable(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.keyframe_button.clicked.connect(self._keyframe_opacity)
        self.content_edit.editingFinished.connect(self._content_changed)
        self.font_edit.editingFinished.connect(self._font_changed)
        self.color_edit.color_changed.connect(lambda _color: self._emit_color_patch())
        self.shadow_color.color_changed.connect(lambda _color: self._emit_shadow_color())
        self.shape_kind.currentIndexChanged.connect(self._shape_kind_changed)
        self.stroke_edit.color_changed.connect(lambda _color: self._shape_style_changed())
        for combo, handler in (
            (self.font_weight, self._combo_style_changed),
            (self.text_align, self._combo_style_changed),
            (self.blend_mode, self._combo_style_changed),
        ):
            combo.currentIndexChanged.connect(lambda _index, h=handler: h())
        self.color_token.currentIndexChanged.connect(lambda _index: self._color_token_changed())
        self.layout_mode.currentIndexChanged.connect(lambda _index: self._layout_combo_changed())
        self.layout_justify.currentIndexChanged.connect(lambda _index: self._layout_combo_changed())
        self.layout_align.currentIndexChanged.connect(lambda _index: self._layout_combo_changed())
        self.constraint_horizontal.currentIndexChanged.connect(lambda _index: self._constraints_combo_changed())
        self.constraint_vertical.currentIndexChanged.connect(lambda _index: self._constraints_combo_changed())
        self.constraint_keep_aspect.currentIndexChanged.connect(lambda _index: self._constraints_combo_changed())
        self.width_mode.currentIndexChanged.connect(lambda _index: self._size_policy_changed())
        self.height_mode.currentIndexChanged.connect(lambda _index: self._size_policy_changed())
        self.canvas_breakpoint.currentIndexChanged.connect(lambda _index: self._canvas_breakpoint_changed())
        style_rows = (
            (self.font_size, "font_size"), (self.line_height, "line_height"),
            (self.letter_spacing, "letter_spacing"), (self.opacity, "opacity"),
            (self.blur, "blur"), (self.shadow_x, "shadow_x"),
            (self.shadow_y, "shadow_y"), (self.shadow_blur, "shadow_blur"),
        )
        for row, field in style_rows:
            row.valueChanged.connect(lambda _value, f=field: self._defer_style_field(f))
        for row, _field in style_rows:
            row.interactionEnded.connect(self._commit_deferred_style_change)
        layout_rows = ((self.layout_gap, "gap"), (self.layout_padding, "padding"),
                       (self.layout_columns, "columns"), (self.layout_rows, "rows"))
        for row, field in layout_rows:
            row.valueChanged.connect(lambda _value, f=field: self._defer_layout_field(f))
        for row, _field in layout_rows:
            row.interactionEnded.connect(self._commit_deferred_layout_change)
        self.stroke_width.valueChanged.connect(lambda _value: self._defer_shape_commit("stroke"))
        self.corner_radius.valueChanged.connect(lambda _value: self._defer_shape_commit("content"))
        self.polygon_sides.valueChanged.connect(lambda _value: self._defer_shape_commit("content"))
        self.star_points.valueChanged.connect(lambda _value: self._defer_shape_commit("content"))
        self.star_inner_ratio.valueChanged.connect(lambda _value: self._defer_shape_commit("content"))
        for control in (self.stroke_width, self.corner_radius, self.polygon_sides, self.star_points, self.star_inner_ratio):
            control.interactionEnded.connect(self._commit_deferred_shape_change)

    def set_frame(self, frame: int) -> None:
        self._frame = int(frame)

    def set_element(self, element: SceneElement | None) -> None:
        self.set_selection((element,) if element is not None else ())

    def set_selection(
        self,
        elements: tuple[SceneElement, ...] | list[SceneElement],
        *,
        tokens: Any = None,
        scene: Any = None,
    ) -> None:
        self._tokens = tokens
        self._scene = scene
        values = tuple(element for element in elements if element is not None)
        self._selection_ids = tuple(element.element_id for element in values)
        self._element = values[0] if values else None
        self._updating = True
        try:
            if not values:
                self.selection_label.setText("")
                self.content_edit.clear()
                self.font_edit.clear()
                self._set_shape_controls(None)
                self._set_text_controls(None)
                self._set_layout_controls(None)
                self._set_constraints_controls(None)
                self._set_appearance_controls(None)
                self._set_transform_controls(None)
                self.animation_summary.setText("")
                return
            self.selection_label.setText(
                f"{len(values)} selected — edits apply to all" if len(values) > 1 else "")
            primary = values[0]
            contents = tuple(self._content_text(item) for item in values)
            self._set_line(self.content_edit, contents[0], mixed=len(set(contents)) > 1)
            self._set_text_controls(primary)
            self._set_layout_controls(primary)
            self._set_constraints_controls(primary)
            self._set_appearance_controls(primary)
            self._set_transform_controls(primary)
            self._set_shape_controls(primary if len(values) == 1 else None)
            self._set_animation_summary(values)
        finally:
            self._updating = False

    def _set_animation_summary(self, values: tuple[SceneElement, ...]) -> None:
        """Explain the selected elements' animation: tracks, easing kinds and
        any motion-plan source (spring config, settle frame, overshoot policy).

        R2: a baked keyframe track must never be the only visible truth — the
        section states what drives it so users (and the audit trail) can tell
        a hand-authored bezier from a compiled spring.
        """
        lines: list[str] = []
        plans_by_element: dict[str, Any] = {}
        scene = getattr(self, "_scene", None)
        if scene is not None:
            try:
                from ..motion.motion_commands import read_motion_plans
                for plan in read_motion_plans(scene).values():
                    for element_id in plan.element_ids:
                        plans_by_element.setdefault(element_id, plan)
            except Exception:  # noqa: BLE001 — display-only path
                plans_by_element = {}
        scene_animations = tuple(getattr(scene, "animations", ()) or ()) if scene is not None else ()
        for element in values:
            track_lines: list[str] = []
            tracks = {track.param_name: track for track in element.local_animation_tracks}
            for animation in scene_animations:
                if animation.element_id == element.element_id:
                    tracks[animation.property] = animation.track
            for prop, track in tracks.items():
                kinds = sorted({str(getattr(key.interpolation, "value", key.interpolation))
                                for key in track.keyframes})
                plan = plans_by_element.get(element.element_id)
                suffix = ""
                if plan is not None and plan.param_name == prop and plan.motion:
                    motion = dict(plan.motion)
                    if motion.get("kind") == "spring":
                        suffix = (
                            f" · spring \"{motion.get('preset_id', plan.preset_id)}\" "
                            f"(damping {motion.get('damping', '?')}, stiffness {motion.get('stiffness', '?')}, "
                            f"settle @ f{motion.get('settle_frame', '?')}, "
                            f"{I18N.tr('motion_graphic.animation.overshoot', 'overshoot') if motion.get('overshoot') else I18N.tr('motion_graphic.animation.clamped', 'clamped')})"
                        )
                    else:
                        suffix = f" · {motion.get('kind', 'easing')} \"{motion.get('preset_id', plan.preset_id)}\""
                track_lines.append(
                    f"{prop} — {len(track.keyframes)} {I18N.tr('motion_graphic.animation.keys', 'keys')}"
                    f" ({', '.join(kinds)}){suffix}"
                )
            if track_lines:
                title = element.element_id if len(values) > 1 else ""
                lines.append((title + "\n" if title else "") + "\n".join(track_lines))
        self.animation_summary.setText("\n".join(lines))

    def _add_section(self, title: str) -> QVBoxLayout:
        section = QFrame(self._host)
        section.setObjectName("editPropertiesSection")
        section.setStyleSheet(EDIT_PROPERTIES_SECTION_STYLE)
        outer = QVBoxLayout(section)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        divider = QFrame(section)
        divider.setFixedHeight(2)
        divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        outer.addWidget(divider)
        header = QWidget(section)
        header.setObjectName("editPropertiesSectionHeader")
        header.setStyleSheet(EDIT_PROPERTIES_SECTION_HEADER_STYLE)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(7, 0, 7, 0)
        collapse = QToolButton(header)
        collapse.setText("▼")
        collapse.setFixedSize(18, 18)
        collapse.setStyleSheet("QToolButton { border: none; color: #9a9a9a; background: transparent; }")
        dot = QLabel("●", header)
        dot.setStyleSheet(f"color: {C.ORANGE}; font-size: 11px;")
        label = QLabel(title, header)
        label.setStyleSheet(EDIT_PROPERTIES_SECTION_TITLE_STYLE)
        header_layout.addWidget(collapse)
        header_layout.addWidget(dot)
        header_layout.addWidget(label)
        header_layout.addStretch(1)
        outer.addWidget(header)
        divider = QFrame(section)
        divider.setFixedHeight(2)
        divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        outer.addWidget(divider)
        content = QWidget(section)
        content.setObjectName("editPropertiesSectionContent")
        content.setStyleSheet(EDIT_PROPERTIES_SECTION_CONTENT_STYLE)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        outer.addWidget(content)
        collapse.clicked.connect(lambda: self._toggle_section(content, collapse))
        self._layout.addWidget(section)
        return layout

    @staticmethod
    def _toggle_section(content: QWidget, button: QToolButton) -> None:
        content.setVisible(not content.isVisible())
        button.setText("▼" if content.isVisible() else "▶")

    def _make_row(self) -> tuple[QWidget, QHBoxLayout]:
        row = QWidget(self._host)
        row.setObjectName("editPropertiesRow")
        row.setStyleSheet(EDIT_PROPERTIES_ROW_STYLE)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)
        return row, layout

    @staticmethod
    def _label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(90)
        return label

    def _add_field_row(self, content: QVBoxLayout, label: str, field: QWidget) -> QWidget:
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        layout.addWidget(field, 1)
        content.addWidget(row)
        return row

    def _add_shape_number_row(
        self, content: QVBoxLayout, label: str, minimum: float, maximum: float,
        value: float, step: float, decimals: int,
    ) -> ParamSliderRow:
        control = ParamSliderRow(
            label, minimum, maximum, value, step=step, decimals=decimals,
            label_width=90, slider_auto_hide=False, value_fixed_width=_VALUE_WIDTH, parent=self._host,
        )
        content.addWidget(control)
        return control

    def _add_style_number_row(self, content: QVBoxLayout, label: str, minimum: float,
                              maximum: float, value: float, step: float, decimals: int) -> ParamSliderRow:
        return self._add_shape_number_row(content, label, minimum, maximum, value, step, decimals)

    def _add_number_row(
        self, content: QVBoxLayout, label: str, minimum: float, maximum: float,
        value: float, decimals: int,
    ) -> DragValue:
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        slider = ToposClickableSlider(Qt.Orientation.Horizontal, parent=row)
        slider.enable_velocity_drag = True
        step = 10.0 ** -decimals
        steps = max(1, round((maximum - minimum) / step))
        slider.setRange(0, steps)
        slider.setValue(round((value - minimum) / step))
        drag = DragValue("", minimum, maximum, value, decimals=decimals, parent=row)
        drag.setSingleStep(step)
        drag.setFixedValueWidth(_VALUE_WIDTH)
        layout.addWidget(slider, 1)
        layout.addWidget(drag)
        content.addWidget(row)

        def from_slider(position: int) -> None:
            drag.blockSignals(True)
            drag.setValue(minimum + position * step)
            drag.blockSignals(False)
            self._defer_transform_change()

        def from_drag(number: float) -> None:
            slider.blockSignals(True)
            slider.setValue(round((number - minimum) / step))
            slider.blockSignals(False)
            self._defer_transform_change()

        slider.valueChanged.connect(from_slider)
        drag.valueChanged.connect(from_drag)
        # AUD-31007: one drag gesture must land as one undoable update, not
        # one per valueChanged tick (same deferral contract as the style,
        # layout and shape rows above).  DragValue wraps every value-changing
        # modality — pointer drag, inline edit, wheel notch — in
        # interaction_started/interaction_ended; the bare slider only reports
        # sliderReleased.
        drag.interaction_ended.connect(self._commit_deferred_transform_change)
        slider.sliderReleased.connect(self._commit_deferred_transform_change)
        return drag

    def _content_changed(self) -> None:
        if self._updating or self._element is None:
            return
        value = self.content_edit.text()
        if len(self._selection_ids) > 1:
            self.selectionUpdateRequested.emit(self._selection_ids, {"content": value})
        elif isinstance(self._element.content, dict):
            content = dict(self._element.content)
            content["text"] = value
            self.elementUpdateRequested.emit(self._element.element_id, {"content": content})
        else:
            self.elementUpdateRequested.emit(self._element.element_id, {"content": value})

    def _font_changed(self) -> None:
        if self._updating or self._element is None:
            return
        self._emit_style_patch({"font_family": self.font_edit.text()})

    def _combo_style_changed(self) -> None:
        if self._updating or self._element is None:
            return
        patch: dict[str, object] = {}
        if self._element.type == "text":
            patch["font_weight"] = str(self.font_weight.currentData())
            patch["text_align"] = str(self.text_align.currentData())
        if self._element.type in {"text", "shape", "frame"}:
            patch["blend_mode"] = str(self.blend_mode.currentData())
        self._emit_style_patch(patch)

    def _populate_color_tokens(self, element: SceneElement) -> None:
        """List the scene palette and reflect the element's binding state."""
        palette = getattr(self._tokens, "palette", None) or {}
        current = self.color_token.blockSignals(True)
        self.color_token.clear()
        self.color_token.addItem(I18N.tr("motion_graphic.tokens.custom_color", "Custom"), "")
        for name in palette:
            self.color_token.addItem(str(name), str(name))
        raw = str(element.style.get("color", element.style.get("fill", "")))
        bound = raw.startswith("$palette.")
        index = self.color_token.findData(raw.partition(".")[2] if bound else "")
        self.color_token.setCurrentIndex(max(0, index))
        self.color_token.blockSignals(current)
        # While bound, the picker previews the resolved token value.
        if bound and self._tokens is not None:
            resolved = self._tokens.resolve_value(raw)
            resolved_color = QColor(str(resolved))
            if resolved_color.isValid():
                self.color_edit.set_rgba(qcolor_to_linear_rgba(resolved_color))

    def _color_token_changed(self) -> None:
        """Bind the layer colour to a palette token or keep a local override."""
        if self._updating or self._element is None:
            return
        name = str(self.color_token.currentData())
        if name:
            ref = f"$palette.{name}"
            self._emit_style_patch({"color": ref, "fill": ref})
        else:
            self._emit_color_patch()

    def _emit_color_patch(self) -> None:
        if self._updating or self._element is None:
            return
        # The picker stores scene-linear RGBA; convert back to sRGB before
        # serialising the hex string the document and shaders share.
        color_text = linear_rgba_to_qcolor(self.color_edit.rgba()).name(QColor.NameFormat.HexRgb)
        # Shape layers render from ``fill`` while text layers render from
        # ``color``.  Persist both colour aliases so the shared Properties
        # control changes the visible result for either native layer type.
        self._emit_style_patch({"color": color_text, "fill": color_text})

    def _emit_shadow_color(self) -> None:
        if self._updating or self._element is None:
            return
        color_text = linear_rgba_to_qcolor(self.shadow_color.rgba()).name(QColor.NameFormat.HexRgb)
        self._emit_style_patch({"shadow_color": color_text})

    def _emit_style_patch(self, patch: dict[str, object]) -> None:
        if self._updating or self._element is None or not patch:
            return
        if len(self._selection_ids) > 1:
            self.selectionUpdateRequested.emit(self._selection_ids, {"style_patch": patch})
        else:
            style = dict(self._element.style)
            style.update(patch)
            self.elementUpdateRequested.emit(self._element.element_id, {"style": style})

    def _defer_style_field(self, field: str) -> None:
        if not self._updating:
            self._pending_style_fields.add(field)

    def _commit_deferred_style_change(self) -> None:
        fields = self._pending_style_fields
        self._pending_style_fields = set()
        if not fields or self._updating or self._element is None:
            return
        patch: dict[str, object] = {}
        if "font_size" in fields:
            patch["font_size"] = self.font_size.value()
        if "line_height" in fields:
            patch["line_height"] = self.line_height.value()
        if "letter_spacing" in fields:
            patch["letter_spacing"] = self.letter_spacing.value()
        if "opacity" in fields:
            patch["opacity"] = round(max(0.0, min(100.0, self.opacity.value())) / 100.0, 4)
        if "blur" in fields:
            patch["blur"] = self.blur.value()
        if "shadow_x" in fields:
            patch["shadow_x"] = self.shadow_x.value()
        if "shadow_y" in fields:
            patch["shadow_y"] = self.shadow_y.value()
        if "shadow_blur" in fields:
            patch["shadow_blur"] = self.shadow_blur.value()
        self._emit_style_patch(patch)

    def _layout_combo_changed(self) -> None:
        if self._updating or self._element is None or self._element.type != "frame":
            return
        mode = str(self.layout_mode.currentData())
        patch: dict[str, object] = {
            "mode": mode,
            "justify": str(self.layout_justify.currentData()),
            "align": str(self.layout_align.currentData()),
        }
        self._emit_layout_patch(patch)
        self._sync_grid_visibility(mode)

    def _sync_grid_visibility(self, mode: str) -> None:
        is_grid = mode == "grid"
        self.layout_columns.setVisible(is_grid)
        self.layout_rows.setVisible(is_grid)

    def _defer_layout_field(self, field: str) -> None:
        if not self._updating:
            self._pending_layout_fields.add(field)

    def _commit_deferred_layout_change(self) -> None:
        fields = self._pending_layout_fields
        self._pending_layout_fields = set()
        if not fields or self._updating or self._element is None or self._element.type != "frame":
            return
        patch: dict[str, object] = {}
        if "gap" in fields:
            patch["gap"] = self.layout_gap.value()
        if "padding" in fields:
            patch["padding"] = self.layout_padding.value()
        if "columns" in fields:
            patch["columns"] = int(round(self.layout_columns.value()))
        if "rows" in fields:
            patch["rows"] = int(round(self.layout_rows.value()))
        self._emit_layout_patch(patch)

    def _emit_layout_patch(self, patch: dict[str, object]) -> None:
        if self._updating or self._element is None or not patch:
            return
        layout = dict(self._element.layout) if isinstance(self._element.layout, Mapping) else {}
        layout.update(patch)
        self.elementUpdateRequested.emit(self._element.element_id, {"layout": layout})

    def _set_layout_controls(self, element: SceneElement | None) -> None:
        is_frame = element is not None and element.type == "frame"
        self._layout_section.setVisible(is_frame)
        if not is_frame or element is None:
            return
        layout = element.layout if isinstance(element.layout, Mapping) else {}
        mode = str(layout.get("mode", "absolute"))
        self.layout_mode.setCurrentIndex(max(0, self.layout_mode.findData(mode)))
        self.layout_gap.setValue(float(layout.get("gap", 0.0)))
        self.layout_padding.setValue(float(layout.get("padding", 0.0)))
        self.layout_justify.setCurrentIndex(max(0, self.layout_justify.findData(str(layout.get("justify", "start")))))
        self.layout_align.setCurrentIndex(max(0, self.layout_align.findData(str(layout.get("align", "stretch")))))
        self.layout_columns.setValue(float(layout.get("columns", 2)))
        self.layout_rows.setValue(float(layout.get("rows", 2)))
        self._sync_grid_visibility(mode)

    # ── R1: 响应式约束与画布 ──────────────────────────────────────

    def _constraints_combo_changed(self) -> None:
        if self._updating or self._element is None:
            return
        patch = {
            "constraints": {
                "horizontal": str(self.constraint_horizontal.currentData()),
                "vertical": str(self.constraint_vertical.currentData()),
                "keep_aspect": bool(self.constraint_keep_aspect.currentData()),
            },
        }
        self._emit_layout_patch(patch)

    def _size_policy_changed(self) -> None:
        if self._updating or self._element is None:
            return
        self._emit_layout_patch({
            "width_mode": str(self.width_mode.currentData()),
            "height_mode": str(self.height_mode.currentData()),
        })

    def _set_constraints_controls(self, element: SceneElement | None) -> None:
        self._constraints_section.setVisible(element is not None)
        if element is None:
            return
        layout = element.layout if isinstance(element.layout, Mapping) else {}
        constraints = layout.get("constraints") if isinstance(layout.get("constraints"), Mapping) else {}
        self.constraint_horizontal.setCurrentIndex(max(
            0, self.constraint_horizontal.findData(str(constraints.get("horizontal", "left")))))
        self.constraint_vertical.setCurrentIndex(max(
            0, self.constraint_vertical.findData(str(constraints.get("vertical", "top")))))
        self.constraint_keep_aspect.setCurrentIndex(max(
            0, self.constraint_keep_aspect.findData(bool(constraints.get("keep_aspect", False)))))
        self.width_mode.setCurrentIndex(max(
            0, self.width_mode.findData(str(layout.get("width_mode", "fixed")))))
        self.height_mode.setCurrentIndex(max(
            0, self.height_mode.findData(str(layout.get("height_mode", "fixed")))))

    def set_scene_canvas(self, width: int, height: int,
                         metadata: Mapping[str, Any] | None = None) -> None:
        """Populate the breakpoint combo from scene state (editor calls this)."""
        from ..core.constraints import read_breakpoints
        self._updating = True
        try:
            combo = self.canvas_breakpoint
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(f"{width} × {height}", (int(width), int(height)))
            for bp in read_breakpoints(metadata):
                combo.addItem(f"{bp.name} ({bp.width} × {bp.height})",
                              (bp.width, bp.height))
            combo.blockSignals(False)
        finally:
            self._updating = False

    def _canvas_breakpoint_changed(self) -> None:
        if self._updating:
            return
        data = self.canvas_breakpoint.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            self.canvasSizeRequested.emit(int(data[0]), int(data[1]))

    def _canvas_ratio_clicked(self, width: int, height: int) -> None:
        self.canvasSizeRequested.emit(int(width), int(height))

    def _set_text_controls(self, element: SceneElement | None) -> None:
        is_text = element is not None and element.type == "text"
        self._text_section.setVisible(is_text)
        # The content editor covers text and other free-content layers; shape
        # geometry lives in the Shape section instead.
        self._content_row.setVisible(element is None or element.type != "shape")
        if not is_text or element is None:
            return
        self._set_line(self.font_edit, str(element.style.get("font_family", "sans-serif")), mixed=False)
        self.font_size.setValue(float(element.style.get("font_size", 48.0)))
        self.font_weight.setCurrentIndex(max(0, self.font_weight.findData(str(element.style.get("font_weight", "normal")))))
        self.text_align.setCurrentIndex(max(0, self.text_align.findData(str(element.style.get("text_align", "left")))))
        self.line_height.setValue(float(element.style.get("line_height", 1.2)))
        self.letter_spacing.setValue(float(element.style.get("letter_spacing", 0.0)))

    def _set_appearance_controls(self, element: SceneElement | None) -> None:
        is_native = element is not None and element.type in {"text", "shape", "frame"}
        self._appearance_section.setVisible(is_native)
        if not is_native or element is None:
            return
        colors = element.style.get("color", element.style.get("fill", "#ffffff"))
        color = QColor(str(colors))
        self.color_edit.set_rgba(qcolor_to_linear_rgba(color if color.isValid() else QColor("#ffffff")))
        self._populate_color_tokens(element)
        self.opacity.setValue(round(float(element.style.get("opacity", 1.0)) * 100.0))
        self.blur.setValue(float(element.style.get("blur", 0.0)))
        self.blend_mode.setCurrentIndex(max(0, self.blend_mode.findData(str(element.style.get("blend_mode", "normal")))))
        self.shadow_x.setValue(float(element.style.get("shadow_x", 0.0)))
        self.shadow_y.setValue(float(element.style.get("shadow_y", 0.0)))
        self.shadow_blur.setValue(float(element.style.get("shadow_blur", 0.0)))
        shadow = QColor(str(element.style.get("shadow_color", "#000000")))
        self.shadow_color.set_rgba(qcolor_to_linear_rgba(shadow if shadow.isValid() else QColor("#000000")))

    def _set_transform_controls(self, element: SceneElement | None) -> None:
        if element is None:
            self.x_spin.setValue(0.0)
            self.y_spin.setValue(0.0)
            self.width_spin.setValue(100.0)
            self.height_spin.setValue(100.0)
            self.scale_spin.setValue(1.0)
            self.rotation_spin.setValue(0.0)
            return
        transform = element.transform
        self.x_spin.setValue(transform.x)
        self.y_spin.setValue(transform.y)
        self.width_spin.setValue(transform.width)
        self.height_spin.setValue(transform.height)
        self.scale_spin.setValue(transform.scale_x)
        self.rotation_spin.setValue(transform.rotation)

    def _set_shape_controls(self, element: SceneElement | None) -> None:
        is_shape = element is not None and element.type == "shape" and isinstance(element.content, dict)
        self._shape_section.setVisible(is_shape)
        if not is_shape:
            return
        content = element.content
        assert isinstance(content, dict)
        kind = str(content.get("kind", "rect"))
        index = self.shape_kind.findData(kind)
        self.shape_kind.setCurrentIndex(max(0, index))
        self.shape_kind.setEnabled(kind != "path")
        stroke = QColor(str(element.style.get("stroke", "#000000")))
        self.stroke_edit.set_rgba(qcolor_to_linear_rgba(stroke if stroke.isValid() else QColor("#000000")))
        self.stroke_width.setValue(float(element.style.get("stroke_width", 0.0)))
        self.corner_radius.setValue(float(content.get("corner_radius", content.get("radius", 0.0))))
        self.polygon_sides.setValue(float(content.get("sides", 6.0)))
        self.star_points.setValue(float(content.get("points", 5.0)))
        self.star_inner_ratio.setValue(float(content.get("inner_ratio", 0.45)))
        self.corner_radius.setVisible(kind == "rounded_rect")
        self.polygon_sides.setVisible(kind == "polygon")
        self.star_points.setVisible(kind == "star")
        self.star_inner_ratio.setVisible(kind == "star")

    def _shape_kind_changed(self, _index: int) -> None:
        if self._updating or self._element is None or not isinstance(self._element.content, dict):
            return
        content = dict(self._element.content)
        content["kind"] = str(self.shape_kind.currentData())
        self.elementUpdateRequested.emit(self._element.element_id, {"content": content})

    def _shape_style_changed(self) -> None:
        if self._updating or self._element is None:
            return
        stroke = linear_rgba_to_qcolor(self.stroke_edit.rgba()).name(QColor.NameFormat.HexRgb)
        style = dict(self._element.style)
        style["stroke"] = stroke
        self.elementUpdateRequested.emit(self._element.element_id, {"style": style})

    def _defer_shape_commit(self, field: str) -> None:
        if not self._updating:
            self._pending_shape_change = field

    def _commit_deferred_shape_change(self) -> None:
        field = self._pending_shape_change
        self._pending_shape_change = None
        if field is None or self._updating or self._element is None or not isinstance(self._element.content, dict):
            return
        if field == "stroke":
            style = dict(self._element.style)
            style["stroke_width"] = self.stroke_width.value()
            self.elementUpdateRequested.emit(self._element.element_id, {"style": style})
            return
        content = dict(self._element.content)
        kind = str(content.get("kind", "rect"))
        if kind == "rounded_rect":
            content["corner_radius"] = self.corner_radius.value()
        elif kind == "polygon":
            content["sides"] = int(round(self.polygon_sides.value()))
        elif kind == "star":
            content["points"] = int(round(self.star_points.value()))
            content["inner_ratio"] = self.star_inner_ratio.value()
        else:
            return
        self.elementUpdateRequested.emit(self._element.element_id, {"content": content})

    def _defer_transform_change(self) -> None:
        """Stage a transform tick; committed once per interaction (AUD-31007)."""
        if not self._updating:
            self._pending_transform_change = True

    def _commit_deferred_transform_change(self) -> None:
        pending = self._pending_transform_change
        self._pending_transform_change = False
        if not pending:
            return
        self._transform_changed()

    def _transform_changed(self) -> None:
        if self._updating or self._element is None:
            return
        transform = SceneTransform(
            x=self.x_spin.value(), y=self.y_spin.value(),
            width=max(1.0, self.width_spin.value()), height=max(1.0, self.height_spin.value()),
            scale_x=self.scale_spin.value(), scale_y=self.scale_spin.value(), rotation=self.rotation_spin.value(),
        )
        if len(self._selection_ids) > 1:
            self.selectionUpdateRequested.emit(
                self._selection_ids,
                {"transform_patch": {"x": transform.x, "y": transform.y,
                                     "width": transform.width, "height": transform.height,
                                     "scale_x": transform.scale_x, "scale_y": transform.scale_y,
                                     "rotation": transform.rotation}},
            )
        else:
            self.elementUpdateRequested.emit(self._element.element_id, {"transform": transform})

    def _keyframe_opacity(self) -> None:
        if self._element is not None:
            # The slider is 0-100 while the animated style property is 0-1;
            # keyframe exactly what the Properties panel currently shows.
            value = round(max(0.0, min(100.0, self.opacity.value())) / 100.0, 4)
            self.keyframeRequested.emit(self._element.element_id, "opacity", value)

    @staticmethod
    def _content_text(element: SceneElement) -> str:
        return str(element.content) if not isinstance(element.content, dict) else str(element.content.get("text", ""))

    @staticmethod
    def _set_line(field: QLineEdit, value: str, *, mixed: bool) -> None:
        field.setPlaceholderText("Mixed values" if mixed else "")
        field.setText("" if mixed else value)
