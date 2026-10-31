"""TTS 合成设置对话框（字幕转语音前采集参数）。

参考 ``CompositeRenderDialog`` 的"采集设置 → get_settings() → controller 执行"模式。
两种合成模式（QTabWidget 切换）：
  - 内置音色：speaker 下拉（9 个内置音色）、language、instruct 语气指令
  - 音色克隆：参考音频文件选择、ref_text、x_vector_only_mode
高级参数区：temperature / top_p / top_k / max_new_tokens（QSlider + 数值显示）。
"""

from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.tts_settings import (
    BUILTIN_LANGUAGES,
    BUILTIN_SPEAKERS,
    DEFAULT_TTS_SETTINGS,
    TtsSettings,
)
from src.shared.export.export_panel import (
    _apply_compact_button_style,
    _apply_text_input_style,
    _make_param_label,
)
from src.shared.i18n.i18n import I18N


class TtsSettingsDialog(QDialog):
    """字幕转语音设置对话框。

    用 ``exec()`` 模态显示，用户确认后用 :meth:`get_settings` 取回设置。
    若所需模型未安装，会在顶部显示红色警告。
    """

    def __init__(
        self,
        parent: Any = None,
        *,
        initial: TtsSettings | None = None,
        model_ready: bool = True,
        missing_hint: str = "",
    ) -> None:
        super().__init__(parent)
        self._settings = (initial or DEFAULT_TTS_SETTINGS)
        self.setWindowTitle(I18N.tr("tts.dialog.title", "文字转语音设置"))
        self.setMinimumWidth(520)
        self._build_ui(model_ready, missing_hint)

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self, model_ready: bool, missing_hint: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 模型未装警告
        if not model_ready:
            warn = QLabel(missing_hint or I18N.tr(
                "tts.dialog.model_missing",
                "⚠️ 所需模型未安装。请先下载模型（见控制台输出的下载命令）。",
            ))
            warn.setStyleSheet("color: #e67e22; font-size: 12px; padding: 6px;")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        # 模式切换 Tab
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_custom_voice_tab(), I18N.tr("tts.dialog.tab.voice", "内置音色"))
        self._tabs.addTab(self._build_voice_clone_tab(), I18N.tr("tts.dialog.tab.clone", "音色克隆"))
        # 根据 initial settings 选中初始 tab
        self._tabs.setCurrentIndex(1 if self._settings.mode == "voice_clone" else 0)
        layout.addWidget(self._tabs)

        # 高级参数
        layout.addWidget(self._build_advanced_group())

        # OK / Cancel
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(I18N.tr("common.cancel", "取消"))
        ok_btn = QPushButton(I18N.tr("common.ok", "确定"))
        ok_btn.setDefault(True)
        _apply_compact_button_style(cancel_btn)
        _apply_compact_button_style(ok_btn)
        ok_btn.setStyleSheet(ok_btn.styleSheet() + "; background-color: #3498db; color: white;")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _build_custom_voice_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        form.setSpacing(10)

        # 音色选择
        self._speaker_combo = QComboBox()
        for name, native_lang, gender, desc in BUILTIN_SPEAKERS:
            label = f"{name} ({desc})"
            self._speaker_combo.addItem(label, name)
        # 选中 initial speaker
        for i in range(self._speaker_combo.count()):
            if self._speaker_combo.itemData(i) == self._settings.speaker:
                self._speaker_combo.setCurrentIndex(i)
                break
        form.addRow(_make_param_label(I18N.tr("tts.dialog.speaker", "音色")), self._speaker_combo)

        # 语言
        self._lang_combo = QComboBox()
        for display, api_value in BUILTIN_LANGUAGES:
            self._lang_combo.addItem(display, api_value)
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == self._settings.language:
                self._lang_combo.setCurrentIndex(i)
                break
        form.addRow(_make_param_label(I18N.tr("tts.dialog.language", "语言")), self._lang_combo)

        # 语气指令 instruct
        self._instruct_edit = QTextEdit()
        self._instruct_edit.setMaximumHeight(70)
        self._instruct_edit.setPlaceholderText(I18N.tr(
            "tts.dialog.instruct_placeholder",
            '可选：输入语气/情感指令，如"用愤怒的语气说"、"轻声细语"',
        ))
        self._instruct_edit.setPlainText(self._settings.instruct)
        _apply_text_input_style(self._instruct_edit)
        form.addRow(_make_param_label(I18N.tr("tts.dialog.instruct", "语气指令")), self._instruct_edit)

        return tab

    def _build_voice_clone_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        form.setSpacing(10)

        # 参考音频文件
        ref_row = QHBoxLayout()
        self._ref_audio_edit = QLineEdit(self._settings.ref_audio_path)
        self._ref_audio_edit.setPlaceholderText(I18N.tr(
            "tts.dialog.ref_audio_placeholder", "选择一段参考音频（wav/mp3，3-10秒最佳）"))
        _apply_text_input_style(self._ref_audio_edit)
        browse_btn = QPushButton(I18N.tr("common.browse", "浏览…"))
        _apply_compact_button_style(browse_btn)
        browse_btn.clicked.connect(self._on_browse_ref_audio)
        ref_row.addWidget(self._ref_audio_edit)
        ref_row.addWidget(browse_btn)
        form.addRow(_make_param_label(I18N.tr("tts.dialog.ref_audio", "参考音频")), ref_row)
        # AUD-21504: 校验失败提示（存在性校验在 _on_accept 中执行）
        self._ref_validation_label = QLabel("")
        self._ref_validation_label.setStyleSheet("color: #e74c3c; font-size: 11px;")
        self._ref_validation_label.setWordWrap(True)
        self._ref_validation_label.hide()
        form.addRow("", self._ref_validation_label)

        # ref_text（参考音频转写文本）
        self._ref_text_edit = QTextEdit()
        self._ref_text_edit.setMaximumHeight(70)
        self._ref_text_edit.setPlaceholderText(I18N.tr(
            "tts.dialog.ref_text_placeholder",
            "输入参考音频对应的文字（ICL 模式必填，克隆质量最佳）"))
        self._ref_text_edit.setPlainText(self._settings.ref_text)
        _apply_text_input_style(self._ref_text_edit)
        form.addRow(_make_param_label(I18N.tr("tts.dialog.ref_text", "参考文本")), self._ref_text_edit)

        # x_vector_only_mode（只用声纹，无需 ref_text）
        self._xvector_check = QCheckBox(I18N.tr(
            "tts.dialog.xvector_only",
            "只用声纹特征（无需参考文本，质量略降）"))
        self._xvector_check.setChecked(self._settings.x_vector_only_mode)
        form.addRow("", self._xvector_check)

        # 语言（克隆模式也需要，用于文本合成）
        self._lang_clone_combo = QComboBox()
        for display, api_value in BUILTIN_LANGUAGES:
            self._lang_clone_combo.addItem(display, api_value)
        for i in range(self._lang_clone_combo.count()):
            if self._lang_clone_combo.itemData(i) == self._settings.language:
                self._lang_clone_combo.setCurrentIndex(i)
                break
        form.addRow(_make_param_label(I18N.tr("tts.dialog.language", "语言")), self._lang_clone_combo)

        return tab

    def _build_advanced_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr("tts.dialog.advanced", "高级参数"))
        form = QFormLayout(group)
        form.setSpacing(8)

        self._temp_slider, self._temp_label = self._make_slider_row(
            self._settings.temperature, 1, 200, 10, "%.1f")
        form.addRow(_make_param_label(I18N.tr("tts.dialog.temperature", "temperature（随机性）")),
                    self._slider_row_widget(self._temp_slider, self._temp_label))

        self._topp_slider, self._topp_label = self._make_slider_row(
            self._settings.top_p, 1, 100, 100, "%.2f")
        form.addRow(_make_param_label(I18N.tr("tts.dialog.top_p", "top_p（核采样）")),
                    self._slider_row_widget(self._topp_slider, self._topp_label))

        # top_k 用 SpinBox
        self._topk_spin = QSpinBox()
        self._topk_spin.setRange(1, 200)
        self._topk_spin.setValue(self._settings.top_k)
        form.addRow(_make_param_label(I18N.tr("tts.dialog.top_k", "top_k")), self._topk_spin)

        # max_new_tokens 用 SpinBox
        self._maxtok_spin = QSpinBox()
        self._maxtok_spin.setRange(256, 8192)
        self._maxtok_spin.setSingleStep(256)
        self._maxtok_spin.setValue(self._settings.max_new_tokens)
        form.addRow(_make_param_label(I18N.tr("tts.dialog.max_tokens", "最大 token 数")), self._maxtok_spin)

        return group

    # ------------------------------------------------------------------ #
    # 辅助：滑块行
    # ------------------------------------------------------------------ #

    def _make_slider_row(
        self, value: float, lo: int, hi: int, scale: int, fmt: str,
    ) -> tuple[QSlider, QLabel]:
        """创建一个水平滑块 + 数值标签。scale 把整数滑块映射到浮点。"""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(int(value * scale))
        label = QLabel(fmt % value)
        label.setMinimumWidth(40)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def _on_change(v: int) -> None:
            label.setText(fmt % (v / scale))

        slider.valueChanged.connect(_on_change)
        return slider, label

    @staticmethod
    def _slider_row_widget(slider: QSlider, label: QLabel) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(slider, 1)
        h.addWidget(label)
        return w

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def _on_browse_ref_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("tts.dialog.select_ref_audio", "选择参考音频"),
            os.path.expanduser("~"),
            I18N.tr("tts.dialog.audio_files", "音频文件 (*.wav *.mp3 *.flac *.ogg);;所有文件 (*)"),
        )
        if path:
            self._ref_audio_edit.setText(path)

    def _show_ref_validation(self, message: str) -> None:
        """AUD-21504: 校验失败给出可见提示并聚焦输入框。"""
        self._ref_validation_label.setText(message)
        self._ref_validation_label.show()
        self._ref_audio_edit.setStyleSheet(
            self._ref_audio_edit.styleSheet()
            + "; border: 1px solid #e74c3c;"
        )
        self._ref_audio_edit.setFocus()

    def _clear_ref_validation(self) -> None:
        """清除校验提示并恢复默认输入框样式。"""
        self._ref_validation_label.hide()
        self._ref_validation_label.setText("")
        _apply_text_input_style(self._ref_audio_edit)

    def _on_accept(self) -> None:
        """校验输入后 accept。"""
        # 音色克隆模式校验参考音频
        if self._tabs.currentIndex() == 1:
            ref_path = self._ref_audio_edit.text().strip()
            if not ref_path:
                self._show_ref_validation(I18N.tr(
                    "tts.dialog.ref_audio_required", "请选择参考音频文件。"))
                return
            # AUD-21504: 路径框支持手动键入（外部输入），必须验证文件真实
            # 存在——不存在的路径透传到 TTS 引擎要到模型加载/合成阶段才
            # 失败，错误远离源头并浪费一次长耗时任务。
            expanded = os.path.abspath(os.path.expanduser(ref_path))
            if not os.path.isfile(expanded):
                self._show_ref_validation(I18N.tr(
                    "tts.dialog.ref_audio_not_found",
                    "参考音频文件不存在：{path}",
                ).format(path=ref_path))
                return
            if not self._xvector_check.isChecked() and not self._ref_text_edit.toPlainText().strip():
                self._ref_text_edit.setFocus()
                return
        self._clear_ref_validation()
        self.accept()

    # ------------------------------------------------------------------ #
    # 取回设置
    # ------------------------------------------------------------------ #

    def get_settings(self) -> TtsSettings:
        """返回对话框采集的 TtsSettings（accept 后调用）。"""
        is_clone = self._tabs.currentIndex() == 1
        # 语言从当前激活 tab 的下拉取
        lang_combo = self._lang_clone_combo if is_clone else self._lang_combo
        language = lang_combo.currentData() or "Auto"

        if is_clone:
            return TtsSettings(
                mode="voice_clone",
                language=language,
                ref_audio_path=self._ref_audio_edit.text().strip(),
                ref_text=self._ref_text_edit.toPlainText().strip(),
                x_vector_only_mode=self._xvector_check.isChecked(),
                temperature=self._temp_slider.value() / 10,
                top_p=self._topp_slider.value() / 100,
                top_k=self._topk_spin.value(),
                max_new_tokens=self._maxtok_spin.value(),
            )
        return TtsSettings(
            mode="custom_voice",
            speaker=self._speaker_combo.currentData() or "Vivian",
            language=language,
            instruct=self._instruct_edit.toPlainText().strip(),
            temperature=self._temp_slider.value() / 10,
            top_p=self._topp_slider.value() / 100,
            top_k=self._topk_spin.value(),
            max_new_tokens=self._maxtok_spin.value(),
        )
