"""Stereo peak meter for the Mixer timeline toolbar."""

from PyQt6.QtWidgets import QWidget

from src.shared.ui.widgets.timeline_output_meter import TimelineOutputMeter


class MixerOutputMeter(TimelineOutputMeter):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MixerTransportMeter")
