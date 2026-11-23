"""Physical scope resolution and bounded resource sizing."""
import pytest
from pytestqt.qtbot import QtBot

from src.features.color.core.scope_types import scope_density_sizes
from src.features.color.ui.scope_panel import ScopePanel, _MODE_PRESENTER_ATTR


@pytest.mark.parametrize(('size', 'mode', 'expected'), [
    ((250, 300), 'parade', ((1024, 300), (800, 800))),
    ((900, 600), 'waveform', ((1024, 640), (800, 800))),
    ((1200, 900), 'quad', ((1216, 960), (960, 960))),
    ((10000, 10000), 'all', ((2048, 1200), (1600, 1600))),
    ((1200, 900), 'histogram', ((1024, 300), (800, 800))),
])
def test_density_sizes_follow_plot_pixels_with_bounded_growth(
    size: tuple[int, int], mode: str, expected: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    assert scope_density_sizes(size, mode) == expected


def test_nearby_resize_pixels_reuse_the_same_density_tier() -> None:
    assert scope_density_sizes((1100, 700), 'split') == scope_density_sizes((1101, 701), 'split')


def test_multiview_demand_uses_each_plot_physical_size(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = ScopePanel()
    qtbot.addWidget(panel)
    panel.resize(960, 680)
    panel.set_mode('quad')
    panel.show()
    panel.layout().activate()
    qtbot.wait(10)
    exposed = [getattr(panel, attr) for attr in _MODE_PRESENTER_ATTR.values()
               if getattr(panel, attr).isVisibleTo(panel)]
    assert len(exposed) == 4
    for widget in exposed:
        monkeypatch.setattr(widget, 'devicePixelRatioF', lambda: 2.0)
    assert panel.demand_state()['display_size'] == (
        max(widget.width() * 2 for widget in exposed),
        max(widget.height() * 2 for widget in exposed),
    )
    assert panel.demand_state()['display_size'][0] < panel.width() * 2


def test_live_panel_resize_coalesces_forced_gpu_demands(qtbot: QtBot) -> None:
    from PyQt6.QtTest import QSignalSpy
    panel = ScopePanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(200)
    changes = QSignalSpy(panel.demand_changed)
    for width in range(500, 601, 5):
        panel.resize(width, 400)
    assert len(changes) == 0
    qtbot.waitUntil(lambda: len(changes) == 1, timeout=1000)
    qtbot.wait(180)
    assert len(changes) == 1
