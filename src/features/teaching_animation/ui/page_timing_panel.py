"""Direct page-deck timing controls for the Motion Graphic editor."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QToolButton,
)

from src.shared.ui.style_sheet import (
    C, EDIT_PROPERTIES_SECTION_CONTENT_STYLE, EDIT_PROPERTIES_SECTION_DIVIDER_STYLE,
    EDIT_PROPERTIES_SECTION_HEADER_STYLE, EDIT_PROPERTIES_SECTION_STYLE,
    EDIT_PROPERTIES_SECTION_TITLE_STYLE,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox, ColorPickerControl, DragValue
from src.shared.ui.widgets.color_picker_control import qcolor_to_linear_rgba
from src.shared.ui.widgets.topos_controls import ToposClickableSlider

from ..core.html_page_deck import (
    PAGE_TRANSITIONS,
    detect_page_count,
    page_start_frame,
    page_timing_for,
    scene_background_css,
)
from ..core.model import TeachingSceneDocument


class PageTimingPanel(QWidget):
    """A visible, per-page timing editor.

    The old timing form lived inside the HTML inspector, which meant it was
    invisible until the synthetic HTML layer row was selected.  This panel is
    independent of layer selection and writes one complete timing command when
    the user presses Apply, so changing several page values remains one undo
    step.
    """

    timingRequested = pyqtSignal(str, float, str)
    transitionRequested = pyqtSignal(str, float)
    settingsRequested = pyqtSignal(str, float, str, str, float)
    settingsWithBackgroundRequested = pyqtSignal(str, float, str, str, float, str, object)
    backgroundSettingsRequested = pyqtSignal(str, object)
    frameRequested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgPageTimingPanel")
        self._scene: TeachingSceneDocument | None = None
        self._timing = None
        self._updating = False
        self._duration_inputs: list[DragValue] = []
        self._page_rows: list[QWidget] = []
        self._page_background_edits: list[QLineEdit] = []
        self._page_background_pickers: list[ColorPickerControl] = []
        self._page_background_inherit: list[QCheckBox] = []

        self._summary = QLabel(I18N.tr("motion_graphic.page.edit_hint", "Select an HTML scene to edit page timing"), self)
        self._summary.setObjectName("mgMutedLabel")

        self._default_seconds = DragValue("", 0.1, 300.0, 5.0, decimals=2, parent=self)
        self._default_seconds.setObjectName("pageTimingDefaultSeconds")
        self._default_seconds.setSingleStep(0.1)
        self._default_seconds.setFixedValueWidth(64)
        self._default_slider = ToposClickableSlider(Qt.Orientation.Horizontal, parent=self)
        self._default_slider.enable_velocity_drag = True
        self._default_slider.setRange(1, 36000)
        self._link_slider(self._default_slider, self._default_seconds, 10.0)
        self._global_background_edit = QLineEdit(self)
        self._global_background_edit.setObjectName("pageGlobalBackground")
        self._global_background_edit.setPlaceholderText(I18N.tr("motion_graphic.page.background_placeholder", "#111827 or CSS color"))
        self._global_background_edit.setClearButtonEnabled(True)
        self._global_background_picker = self._make_background_picker(self._global_background_edit)
        self._custom = QCheckBox(I18N.tr("motion_graphic.page.per_page_duration", "Set duration for each page"), self)
        self._custom.setObjectName("pageTimingCustom")
        self._custom.toggled.connect(self._set_custom_enabled)

        settings = QVBoxLayout()
        settings.setContentsMargins(0, 0, 0, 0)
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel(I18N.tr("motion_graphic.page.default_duration", "Default page"), self))
        default_row.addWidget(self._default_slider, 1)
        default_row.addWidget(self._default_seconds)
        settings.addLayout(default_row)
        background_row = QHBoxLayout()
        background_row.addWidget(QLabel(I18N.tr("motion_graphic.page.global_background", "Global background"), self))
        background_row.addWidget(self._global_background_edit, 1)
        background_row.addWidget(self._global_background_picker)
        settings.addLayout(background_row)
        settings.addWidget(self._custom)

        self._page_host = QWidget(self)
        self._page_layout = QVBoxLayout(self._page_host)
        self._page_layout.setContentsMargins(0, 0, 0, 0)
        self._page_layout.setSpacing(3)
        self._page_layout.addStretch(1)
        self._page_scroll = QScrollArea(self)
        self._page_scroll.setObjectName("pageTimingScroll")
        self._page_scroll.setWidgetResizable(True)
        self._page_scroll.setWidget(self._page_host)
        self._page_list_label = QLabel(I18N.tr("motion_graphic.page.page_list", "Pages (click to jump)"), self)

        # Use the same project selector shown in Edit > Properties.  It draws
        # the chevron itself but keeps Qt's application-owned style intact,
        # which is safe when editor tabs are created and destroyed on macOS.
        transition = ChevronComboBox(self)
        transition.setObjectName("pageTimingTransition")
        labels = {
            "cut": I18N.tr("motion_graphic.page.cut", "Cut"),
            "crossfade": I18N.tr("motion_graphic.page.crossfade", "Crossfade"),
            "slide-left": I18N.tr("motion_graphic.page.slide_left", "Slide Left"),
            "slide-right": I18N.tr("motion_graphic.page.slide_right", "Slide Right"),
            "slide-up": I18N.tr("motion_graphic.page.slide_up", "Slide Up"),
            "slide-down": I18N.tr("motion_graphic.page.slide_down", "Slide Down"),
            "zoom": I18N.tr("motion_graphic.page.zoom", "Zoom"),
            "wipe-left": I18N.tr("motion_graphic.page.wipe_left", "Wipe Left"),
            "wipe-right": I18N.tr("motion_graphic.page.wipe_right", "Wipe Right"),
            "wipe-up": I18N.tr("motion_graphic.page.wipe_up", "Wipe Up"),
            "wipe-down": I18N.tr("motion_graphic.page.wipe_down", "Wipe Down"),
        }
        for item in PAGE_TRANSITIONS:
            transition.addItem(labels.get(item, item), item)
        self._transition = transition
        self._transition_seconds = DragValue("", 0.0, 5.0, 0.0, decimals=2, parent=self)
        self._transition_seconds.setObjectName("pageTimingTransitionSeconds")
        self._transition_seconds.setSingleStep(0.05)
        self._transition_seconds.setFixedValueWidth(64)
        self._transition_slider = ToposClickableSlider(Qt.Orientation.Horizontal, parent=self)
        self._transition_slider.enable_velocity_drag = True
        self._transition_slider.setRange(0, 500)
        self._link_slider(self._transition_slider, self._transition_seconds, 100.0)
        transition_form = QVBoxLayout()
        transition_form.setContentsMargins(0, 0, 0, 0)
        transition_row = QHBoxLayout()
        transition_row.addWidget(QLabel(I18N.tr("motion_graphic.page.transition", "Transition"), self))
        transition_row.addWidget(self._transition, 1)
        transition_form.addLayout(transition_row)
        transition_duration_row = QHBoxLayout()
        transition_duration_row.addWidget(QLabel(I18N.tr("motion_graphic.page.transition_duration", "Transition Duration"), self))
        transition_duration_row.addWidget(self._transition_slider, 1)
        transition_duration_row.addWidget(self._transition_seconds)
        transition_form.addLayout(transition_duration_row)
        # Page duration and page transition are one configuration surface.
        # A single concise action applies both values, so labels never truncate
        # in the narrow inspector and the user does not need two clicks.
        self._apply_button = QPushButton(I18N.tr("motion_graphic.page.apply", "Apply"), self)
        self._apply_button.setObjectName("mgPageTimingApplyButton")
        self._apply_button.setFixedWidth(72)
        self._apply_button.clicked.connect(self._emit_all)
        # Compatibility aliases retain the prior private test/integration
        # surface while intentionally pointing to the sole visible button.
        self._apply_timing = self._apply_button
        self._apply_transition = self._apply_button
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 2, 0, 0)
        action_row.addStretch(1)
        action_row.addWidget(self._apply_button)

        # Use the same collapsible Properties section shell as HTML Tokens.
        # The group therefore participates in the inspector's normal scroll
        # flow instead of being a permanently pinned footer.
        self.setObjectName("editPropertiesSection")
        self.setStyleSheet(EDIT_PROPERTIES_SECTION_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top_divider = QWidget(self)
        top_divider.setFixedHeight(2)
        top_divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        outer.addWidget(top_divider)
        header = QWidget(self)
        header.setObjectName("editPropertiesSectionHeader")
        header.setStyleSheet(EDIT_PROPERTIES_SECTION_HEADER_STYLE)
        header.setFixedHeight(24)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(7, 0, 7, 0)
        header_layout.setSpacing(4)
        self._collapse = QToolButton(header)
        self._collapse.setText("▼")
        self._collapse.setFixedSize(18, 18)
        self._collapse.setStyleSheet(
            "QToolButton { border: none; color: #9a9a9a; background: transparent; }",
        )
        dot = QLabel("●", header)
        dot.setStyleSheet(f"color: {C.ORANGE}; font-size: 11px;")
        title = QLabel(I18N.tr("motion_graphic.sections.page", "Page"), header)
        title.setStyleSheet(EDIT_PROPERTIES_SECTION_TITLE_STYLE)
        header_layout.addWidget(self._collapse)
        header_layout.addWidget(dot)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        outer.addWidget(header)
        divider = QWidget(self)
        divider.setFixedHeight(2)
        divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        outer.addWidget(divider)
        self._content = QWidget(self)
        self._content.setObjectName("editPropertiesSectionContent")
        self._content.setStyleSheet(EDIT_PROPERTIES_SECTION_CONTENT_STYLE)
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        layout.addWidget(self._summary)
        layout.addLayout(settings)
        layout.addWidget(self._page_list_label)
        self._page_scroll.setMaximumHeight(220)
        layout.addWidget(self._page_scroll)
        layout.addLayout(transition_form)
        layout.addLayout(action_row)
        outer.addWidget(self._content)
        self._collapse.clicked.connect(self._toggle_content)
        self.setVisible(False)

    def _toggle_content(self) -> None:
        # ``isVisible`` includes the hidden editor ancestor in headless/test
        # construction.  The explicit hidden flag is the actual collapse
        # state and behaves identically before and after the window is shown.
        self._content.setVisible(self._content.isHidden())
        self._collapse.setText("▼" if not self._content.isHidden() else "▶")

    def set_scene(self, scene: TeachingSceneDocument) -> None:
        layer = scene.html_layer
        if layer is None:
            self._scene = None
            self._timing = None
            self._clear_pages()
            self.setVisible(False)
            return
        page_count = detect_page_count(layer.sanitized_html)
        timing = page_timing_for(
            getattr(layer, "metadata", {}),
            page_count=page_count,
            fallback_default_seconds=(
                max(0.001, scene.duration_frames / max(0.001, scene.fps))
                / page_count
            ),
        )
        self._scene = scene
        self._timing = timing
        self._updating = True
        try:
            self._clear_pages()
            self._default_seconds.setValue(timing.default_seconds)
            global_background = scene_background_css(scene.background)
            self._global_background_edit.setText(global_background)
            self._global_background_picker.set_rgba(
                qcolor_to_linear_rgba(self._color_from_css(global_background)))
            self._custom.setChecked(timing.mode == "custom")
            transition_index = self._transition.findData(timing.transition)
            self._transition.setCurrentIndex(max(0, transition_index))
            self._transition_seconds.setValue(timing.transition_seconds)
            for index, seconds in enumerate(timing.durations):
                wrapper = QWidget(self._page_host)
                row_layout = QHBoxLayout()
                row_layout.setContentsMargins(0, 0, 0, 0)
                jump = QToolButton(wrapper)
                jump.setObjectName("mgPageTimingJump")
                jump.setText(I18N.tr("motion_graphic.page.page_button", "Page {index}", index=index + 1))
                jump.setToolTip(I18N.tr("motion_graphic.page.jump_to_page", "Jump to this page"))
                jump.clicked.connect(lambda _checked=False, i=index: self._jump_to_page(i))
                start = page_start_frame(index, scene.fps, timing) / max(0.001, scene.fps)
                end = start + float(seconds)
                range_label = QLabel(f"{start:g}–{end:g}s", wrapper)
                range_label.setObjectName("mgMutedLabel")
                value = DragValue("", 0.1, 300.0, seconds, decimals=2, parent=wrapper)
                value.setObjectName(f"pageTimingSeconds{index + 1}")
                value.setSingleStep(0.1)
                value.setFixedValueWidth(64)
                self._duration_inputs.append(value)
                row_layout.addWidget(jump)
                row_layout.addWidget(range_label, 1)
                row_layout.addWidget(value)

                background_row = QHBoxLayout()
                background_label = QLabel(
                    I18N.tr("motion_graphic.page.background", "Background"), wrapper,
                )
                background_label.setObjectName("mgMutedLabel")
                inherit = QCheckBox(
                    I18N.tr("motion_graphic.page.inherit_background", "Use global"),
                    wrapper,
                )
                inherit.setObjectName(f"pageBackgroundInherit{index + 1}")
                page_background = scene.pages[index].background if index < len(scene.pages) else ""
                inherit.setChecked(not bool(str(page_background).strip()))
                edit = QLineEdit(str(page_background), wrapper)
                edit.setObjectName(f"pageBackground{index + 1}")
                edit.setPlaceholderText(global_background)
                edit.setClearButtonEnabled(True)
                picker = self._make_background_picker(edit, wrapper)
                picker.set_rgba(qcolor_to_linear_rgba(
                    self._color_from_css(page_background or global_background)))
                picker.setEnabled(not inherit.isChecked())
                inherit.toggled.connect(picker.setDisabled)
                inherit.toggled.connect(edit.setDisabled)
                background_row.addWidget(background_label)
                background_row.addWidget(inherit)
                background_row.addWidget(edit, 1)
                background_row.addWidget(picker)

                wrapper_layout = QVBoxLayout(wrapper)
                wrapper_layout.setContentsMargins(0, 0, 0, 2)
                wrapper_layout.setSpacing(1)
                wrapper_layout.addLayout(row_layout)
                wrapper_layout.addLayout(background_row)
                self._page_rows.append(wrapper)
                self._page_background_edits.append(edit)
                self._page_background_pickers.append(picker)
                self._page_background_inherit.append(inherit)
                self._page_layout.insertWidget(self._page_layout.count() - 1, wrapper)
            self._set_custom_enabled(timing.mode == "custom")
            # A single page has no per-page list to customize.  Hiding those
            # controls removes the large empty gap and leaves two compact,
            # immediately understandable groups: duration and transition.
            multiple_pages = page_count > 1
            self._custom.setVisible(multiple_pages)
            self._page_list_label.setVisible(multiple_pages)
            self._page_scroll.setVisible(multiple_pages)
            self._summary.setText(I18N.tr(
                "motion_graphic.page.page_summary", "{count} pages · Total {seconds:g} s · {fps:g} fps",
                count=page_count, seconds=timing.total_seconds, fps=scene.fps,
            ))
            self.setVisible(True)
        finally:
            self._updating = False

    def _clear_pages(self) -> None:
        self._duration_inputs = []
        self._page_background_edits = []
        self._page_background_pickers = []
        self._page_background_inherit = []
        for row in self._page_rows:
            self._page_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._page_rows = []

    def _set_custom_enabled(self, enabled: bool) -> None:
        for value in self._duration_inputs:
            value.setEnabled(bool(enabled))

    def _jump_to_page(self, index: int) -> None:
        if self._scene is None or self._timing is None:
            return
        frame = page_start_frame(index, self._scene.fps, self._timing)
        self.frameRequested.emit(max(0, min(self._scene.duration_frames - 1, frame)))

    def _emit_timing(self) -> None:
        if self._scene is None or self._timing is None or self._updating:
            return
        durations = ", ".join(f"{value.value():g}" for value in self._duration_inputs)
        self.timingRequested.emit(
            "custom" if self._custom.isChecked() else "default",
            self._default_seconds.value(),
            durations,
        )

    def _emit_all(self) -> None:
        """Apply page durations and transition from the single visible action."""
        if self._scene is None or self._timing is None or self._updating:
            return
        mode = "custom" if self._custom.isChecked() else "default"
        durations = ", ".join(f"{value.value():g}" for value in self._duration_inputs)
        transition = str(self._transition.currentData() or "cut")
        seconds = self._transition_seconds.value()
        backgrounds = tuple(
            "" if inherit.isChecked() else edit.text().strip()
            for inherit, edit in zip(self._page_background_inherit, self._page_background_edits)
        )
        # Backgrounds are emitted first because the connected command refreshes
        # this panel.  Applying timing first would rebuild these controls from
        # the old scene and lose the just-entered background values before the
        # second signal was delivered.
        self.backgroundSettingsRequested.emit(
            self._global_background_edit.text().strip(), backgrounds,
        )
        self.settingsWithBackgroundRequested.emit(
            mode,
            self._default_seconds.value(),
            durations,
            transition,
            seconds,
            self._global_background_edit.text().strip(),
            backgrounds,
        )
        # Retain legacy notification signals for integrations that consume the
        # panel directly.  The editor listens to the combined signal above and
        # writes the whole form as one command/one undo item.
        self.timingRequested.emit(mode, self._default_seconds.value(), durations)
        self.transitionRequested.emit(transition, seconds)
        self.settingsRequested.emit(mode, self._default_seconds.value(), durations, transition, seconds)

    def _make_background_picker(
        self, edit: QLineEdit, parent: QWidget | None = None,
    ) -> ColorPickerControl:
        picker = ColorPickerControl(
            qcolor_to_linear_rgba(QColor("#111827")), parent or self,
        )
        picker.setFixedWidth(82)
        picker.color_changed.connect(lambda color: edit.setText(color.name()))
        return picker

    @staticmethod
    def _color_from_css(value: object) -> QColor:
        color = QColor(str(value or ""))
        return color if color.isValid() else QColor("#111827")

    def _emit_transition(self) -> None:
        if self._scene is None or self._timing is None or self._updating:
            return
        self.transitionRequested.emit(
            str(self._transition.currentData() or "cut"),
            self._transition_seconds.value(),
        )

    @staticmethod
    def _link_slider(slider: ToposClickableSlider, value: DragValue, factor: float) -> None:
        """Keep Properties-style slider and drag value synchronized.

        AUD-31003: the mirror writes are wrapped in paired ``blockSignals``
        guards (same idiom as SceneInspector._add_number_row) so the
        slider→value→slider echo loop is cut at the source instead of
        relying on both controls' value-equality short circuits to
        converge. Nothing else in this panel listens to the mirrored
        control's ``valueChanged`` — the applied values are read directly
        in ``_emit_all`` — so suppressing the mirrored emission loses
        nothing.
        """

        def _slider_to_value(position: int) -> None:
            guard = value.blockSignals(True)
            try:
                value.setValue(position / factor)
            finally:
                value.blockSignals(guard)

        def _value_to_slider(number: float) -> None:
            guard = slider.blockSignals(True)
            try:
                slider.setValue(round(number * factor))
            finally:
                slider.blockSignals(guard)

        slider.valueChanged.connect(_slider_to_value)
        value.valueChanged.connect(_value_to_slider)


__all__ = ["PageTimingPanel"]
