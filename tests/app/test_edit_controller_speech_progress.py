"""Tests for speech-to-text status-bar progress feedback."""

from __future__ import annotations

from src.app.controllers.edit_controller import EditControllerMixin


class _ProgressBar:
    def __init__(self) -> None:
        self.range: tuple[int, int] | None = None
        self.value: int | None = None
        self.visible = False

    def setRange(self, minimum: int, maximum: int) -> None:
        self.range = (minimum, maximum)

    def setValue(self, value: int) -> None:
        self.value = value

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class _StatusBar:
    def __init__(self) -> None:
        self.message: tuple[str, int] | None = None

    def showMessage(self, message: str, timeout: int) -> None:
        self.message = (message, timeout)


def test_speech_progress_uses_indeterminate_bar_for_unknown_duration() -> None:
    controller = EditControllerMixin()
    controller.progress_bar = _ProgressBar()
    controller.status_bar = _StatusBar()

    controller._on_speech_to_text_progress(None, "正在运行语音识别模型")

    assert controller.progress_bar.range == (0, 0)
    assert controller.progress_bar.visible is True
    assert controller.status_bar.message == ("语音转文字：正在运行语音识别模型", 0)


def test_speech_progress_switches_to_percentage_and_cleans_up() -> None:
    controller = EditControllerMixin()
    controller.progress_bar = _ProgressBar()
    controller.status_bar = _StatusBar()

    controller._on_speech_to_text_progress(0.42, "正在整理时间轴")
    assert controller.progress_bar.range == (0, 100)
    assert controller.progress_bar.value == 42

    controller._on_speech_to_text_finished()
    assert controller.progress_bar.visible is False
    assert controller.progress_bar.range == (0, 100)
    assert controller._speech_to_text_worker is None
